"""Collapsible personal-AWS 'alpha' phase provisioner, runner, site deployer, and teardown CLI.

From the repository root:
    ./leaderboard/aws_alpha.sh up --region us-east-1
    ./leaderboard/aws_alpha.sh run --preset mini --systems "dolt dolt_mysql"
    ./leaderboard/aws_alpha.sh deploy-site
    ./leaderboard/aws_alpha.sh status
    ./leaderboard/aws_alpha.sh collapse

Every AWS resource created for the alpha phase is tagged with:
    Project=BranchBenchLeaderboard
    Phase=alpha
    Stack=<stack>   (default: branchbench-alpha)
and recorded in leaderboard/.runs/aws_alpha_state.json.

Running `collapse` (or `down` / `destroy`) terminates the EC2 worker, waits for
termination, deletes any leftover EBS volumes, deletes the security group and key pair,
empties and deletes the alpha S3 bucket, and removes leaderboard/.site/alpha and the
local state file so ongoing AWS cost returns to $0.00.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

from leaderboard import site
from leaderboard.validate import BOARD, MACHINE_RE

ROOT = BOARD.parent
RUNS = BOARD / ".runs"
DEFAULT_STACK = "branchbench-alpha"
DEFAULT_REGION = "us-east-1"
DEFAULT_INSTANCE_TYPE = "i4i.2xlarge"
DEFAULT_ROOT_GB = 100
STACK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
SSM_UBUNTU_2404_AMD64 = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
)
HOSTED_ENV_KEYS = (
    "NEON_API_KEY_ORG",
    "TIGER_ACCESS_KEY",
    "TIGER_SECRET_KEY",
    "TIGER_PROJECT_ID",
    "XATA_API_KEY",
    "XATA_ORGANIZATION_ID",
    "MO_ARCHIVE_URL",
    "SEEKDB_ARCHIVE_URL",
    "CHRONOS_PIP_SPEC",
)


class AlphaError(RuntimeError):
    """Actionable error during AWS alpha phase lifecycle operations."""


def validate_stack(stack: str) -> str:
    if not isinstance(stack, str) or not STACK_RE.fullmatch(stack):
        raise AlphaError(
            f"invalid --stack {stack!r}: must match {STACK_RE.pattern} (lowercase letters, digits, hyphens)"
        )
    return stack


def default_machine_label(instance_type: str, region: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", f"aws-alpha-{instance_type}-{region}").strip("-")
    return slug[:64]


def tag_spec(resource_type: str, stack: str, name: str | None = None) -> str:
    tags = [
        f"{{Key=Project,Value=BranchBenchLeaderboard}}",
        f"{{Key=Phase,Value=alpha}}",
        f"{{Key=Stack,Value={stack}}}",
        f"{{Key=Name,Value={name or stack}}}",
    ]
    return f"ResourceType={resource_type},Tags=[{','.join(tags)}]"


def state_file_path(runs_dir: Path = RUNS, stack: str = DEFAULT_STACK) -> Path:
    validate_stack(stack)
    if stack == DEFAULT_STACK:
        return runs_dir / "aws_alpha_state.json"
    return runs_dir / f"aws_alpha_{stack}.json"


def key_file_path(runs_dir: Path = RUNS, stack: str = DEFAULT_STACK) -> Path:
    validate_stack(stack)
    return runs_dir / f"{stack}.pem"


def load_state(runs_dir: Path = RUNS, stack: str = DEFAULT_STACK) -> dict[str, Any]:
    path = state_file_path(runs_dir, stack)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AlphaError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AlphaError(f"state file {path} must contain a JSON object")
    return data


def save_state(state: dict[str, Any], runs_dir: Path = RUNS, stack: str = DEFAULT_STACK) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = state_file_path(runs_dir, stack)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def detect_caller_cidr(
    explicit_cidr: str = "",
    http_getter: Callable[[str], str] | None = None,
) -> str:
    if explicit_cidr.strip():
        try:
            net = ipaddress.ip_network(explicit_cidr.strip(), strict=False)
        except ValueError as exc:
            raise AlphaError(f"invalid --ssh-cidr {explicit_cidr!r}: {exc}") from exc
        if net.version != 4:
            raise AlphaError(f"--ssh-cidr must be an IPv4 CIDR, got {explicit_cidr!r}")
        return str(net)
    getter = http_getter or _default_http_get
    try:
        raw_ip = getter("https://checkip.amazonaws.com").strip()
        addr = ipaddress.ip_address(raw_ip)
        if addr.version != 4:
            raise ValueError("expected IPv4 address")
        return f"{addr}/32"
    except Exception as exc:
        raise AlphaError(
            f"could not auto-detect caller public IPv4 ({exc}); pass --ssh-cidr <IP>/32 explicitly"
        ) from exc


def _default_http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode("utf-8", errors="replace")


def bootstrap_user_data() -> str:
    """Cloud-init script for Ubuntu 24.04 x86_64 worker VM."""
    return """#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

cat >/etc/sudoers.d/branchbench-shutdown <<'EOF'
ubuntu ALL=(root) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown
EOF
chmod 0440 /etc/sudoers.d/branchbench-shutdown

# Arm dead-man auto-poweroff timer (6 hours) before network/package operations
/sbin/shutdown -P +360 >/dev/null 2>&1

systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true

apt-get update -y
apt-get install -y --no-install-recommends \\
  ca-certificates curl git jq rsync build-essential pkg-config gnupg lsb-release \\
  protobuf-compiler xfsprogs nvme-cli docker.io \\
  mysql-client

install -d /usr/share/postgresql-common/pgdg
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
codename="$(lsb_release -cs)"
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \\
  >/etc/apt/sources.list.d/pgdg.list
apt-get update -y
apt-get install -y --no-install-recommends postgresql-18 postgresql-client-18

for unit in postgresql mysql mariadb; do
  systemctl disable --now "$unit" >/dev/null 2>&1 || true
done

usermod -aG docker ubuntu || true
systemctl enable --now docker

mkdir -p /mnt/nvme
ROOT_DISK="$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -n1 || true)"
NVME_DEV=""
for dev in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{print $1}'); do
  if [ "$dev" != "$ROOT_DISK" ] && ! mountpoint -q "/dev/$dev" && [ -z "$(lsblk -no MOUNTPOINT "/dev/$dev" | tr -d '[:space:]')" ]; then
    NVME_DEV="/dev/$dev"
    break
  fi
done

if [ -n "$NVME_DEV" ]; then
  mkfs.xfs -f -m crc=1,reflink=1 "$NVME_DEV"
  mount -o noatime "$NVME_DEV" /mnt/nvme
else
  # Fallback for non-NVMe instance types (e.g. c6i.2xlarge): loopback XFS with reflink=1
  fallocate -l 40G /var/lib/branchbench-xfs.img
  mkfs.xfs -f -m crc=1,reflink=1 /var/lib/branchbench-xfs.img
  mount -o loop,noatime /var/lib/branchbench-xfs.img /mnt/nvme
fi
chown -R ubuntu:ubuntu /mnt/nvme

su - ubuntu -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
touch /var/lib/branchbench-alpha-ready
"""


class AwsCli:
    """Thin wrapper around the `aws` CLI with region and dry-run handling."""

    def __init__(
        self,
        region: str = DEFAULT_REGION,
        profile: str = "",
        dry_run: bool = False,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.region = region
        self.profile = profile
        self.dry_run = dry_run
        self.runner = runner

    def base_cmd(self, include_region: bool = True) -> list[str]:
        if shutil.which("aws") is None and self.runner is subprocess.run:
            raise AlphaError("AWS CLI ('aws') is not installed or not on PATH")
        cmd = ["aws"]
        if include_region and self.region:
            cmd.extend(["--region", self.region])
        if self.profile:
            cmd.extend(["--profile", self.profile])
        return cmd

    def call_json(self, args: list[str], *, include_region: bool = True) -> Any:
        cmd = [*self.base_cmd(include_region=include_region), *args, "--output", "json"]
        res = self.runner(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise AlphaError(f"aws {' '.join(args[:2])} failed: {detail or f'exit {res.returncode}'}")
        out = (res.stdout or "").strip()
        return json.loads(out) if out else {}

    def call_text(self, args: list[str], *, include_region: bool = True, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [*self.base_cmd(include_region=include_region), *args, "--output", "text"]
        res = self.runner(cmd, capture_output=True, text=True)
        if check and res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise AlphaError(f"aws {' '.join(args[:2])} failed: {detail or f'exit {res.returncode}'}")
        return res


def caller_identity(aws: AwsCli) -> dict[str, str]:
    data = aws.call_json(["sts", "get-caller-identity"], include_region=False)
    account = str(data.get("Account") or "").strip()
    arn = str(data.get("Arn") or "").strip()
    if not account:
        raise AlphaError("aws sts get-caller-identity returned no Account ID")
    return {"account_id": account, "arn": arn}


def resolve_ubuntu_ami(aws: AwsCli, explicit_ami: str = "") -> str:
    if explicit_ami.strip():
        return explicit_ami.strip()
    res = aws.call_text(
        ["ssm", "get-parameter", "--name", SSM_UBUNTU_2404_AMD64, "--query", "Parameter.Value"],
        check=False,
    )
    ami = (res.stdout or "").strip()
    if res.returncode == 0 and ami.startswith("ami-"):
        return ami
    images = aws.call_json(
        [
            "ec2",
            "describe-images",
            "--owners",
            "099720109477",
            "--filters",
            "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*",
            "Name=state,Values=available",
        ]
    )
    candidates = sorted(
        images.get("Images") or [],
        key=lambda img: str(img.get("CreationDate") or ""),
        reverse=True,
    )
    if not candidates or not candidates[0].get("ImageId"):
        raise AlphaError(f"could not find an Ubuntu 24.04 amd64 AMI in {aws.region}; pass --ami-id explicitly")
    return str(candidates[0]["ImageId"])


def default_bucket_name(stack: str, account_id: str, region: str) -> str:
    name = f"{stack}-{account_id}-{region}".lower()
    name = re.sub(r"[^a-z0-9.-]+", "-", name).strip("-.")
    return name[:63]


def discover_stack_resources(aws: AwsCli, stack: str, state: dict[str, Any]) -> dict[str, Any]:
    """Find all AWS resources belonging to (Phase=alpha, Stack=<stack>), merged with state."""
    validate_stack(stack)
    tag_filters = [
        "Name=tag:Project,Values=BranchBenchLeaderboard",
        "Name=tag:Phase,Values=alpha",
        f"Name=tag:Stack,Values={stack}",
    ]

    inst_resp = aws.call_json(["ec2", "describe-instances", "--filters", *tag_filters])
    instances: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    for resv in inst_resp.get("Reservations") or []:
        for inst in resv.get("Instances") or []:
            iid = str(inst.get("InstanceId") or "")
            st = str((inst.get("State") or {}).get("Name") or "")
            if iid and st != "terminated":
                seen_instances.add(iid)
                instances.append(
                    {
                        "instance_id": iid,
                        "state": st,
                        "instance_type": str(inst.get("InstanceType") or ""),
                        "public_ip": str(inst.get("PublicIpAddress") or ""),
                    }
                )
    state_iid = str(state.get("instance_id") or "")
    if state_iid and state_iid not in seen_instances:
        extra = aws.call_json(["ec2", "describe-instances", "--instance-ids", state_iid])
        for resv in extra.get("Reservations") or []:
            for inst in resv.get("Instances") or []:
                st = str((inst.get("State") or {}).get("Name") or "")
                if st != "terminated":
                    instances.append(
                        {
                            "instance_id": state_iid,
                            "state": st,
                            "instance_type": str(inst.get("InstanceType") or ""),
                            "public_ip": str(inst.get("PublicIpAddress") or ""),
                        }
                    )

    vol_resp = aws.call_json(["ec2", "describe-volumes", "--filters", *tag_filters])
    volumes = [
        {
            "volume_id": str(v.get("VolumeId") or ""),
            "state": str(v.get("State") or ""),
        }
        for v in (vol_resp.get("Volumes") or [])
        if v.get("VolumeId")
    ]

    sg_resp = aws.call_json(["ec2", "describe-security-groups", "--filters", *tag_filters])
    sgs = [str(g.get("GroupId") or "") for g in (sg_resp.get("SecurityGroups") or []) if g.get("GroupId")]
    state_sg = str(state.get("security_group_id") or "")
    if state_sg and state_sg not in sgs:
        sgs.append(state_sg)

    kp_resp = aws.call_json(["ec2", "describe-key-pairs", "--filters", *tag_filters])
    keys = [str(k.get("KeyName") or "") for k in (kp_resp.get("KeyPairs") or []) if k.get("KeyName")]
    state_key = str(state.get("key_name") or "")
    if state_key and state_key not in keys:
        keys.append(state_key)

    buckets: list[str] = []
    unverified_buckets: list[str] = []
    bucket_discovery_error = ""
    state_bucket = str(state.get("bucket") or "")
    if state_bucket:
        buckets.append(state_bucket)
    try:
        b_resp = aws.call_json(["s3api", "list-buckets"])
        for b_item in b_resp.get("Buckets") or []:
            b_name = str((b_item or {}).get("Name") or "")
            if not b_name or b_name in buckets:
                continue
            ownership = _inspect_bucket_ownership(aws, stack, b_name)
            if ownership == "owned":
                buckets.append(b_name)
            elif ownership == "unknown":
                unverified_buckets.append(b_name)
    except AlphaError as exc:
        bucket_discovery_error = str(exc)
    bucket = buckets[0] if buckets else ""
    return {
        "stack": stack,
        "region": aws.region,
        "instances": instances,
        "volumes": volumes,
        "security_groups": sgs,
        "key_pairs": keys,
        "bucket": bucket,
        "buckets": buckets,
        "unverified_buckets": unverified_buckets,
        "bucket_discovery_error": bucket_discovery_error,
    }


def ensure_key_pair(aws: AwsCli, stack: str, runs_dir: Path) -> tuple[str, Path]:
    key_name = f"{stack}-key"
    pem_path = key_file_path(runs_dir, stack)
    runs_dir.mkdir(parents=True, exist_ok=True)
    existing = aws.call_json(
        ["ec2", "describe-key-pairs", "--filters", f"Name=key-name,Values={key_name}"]
    ).get("KeyPairs") or []
    if existing and pem_path.is_file():
        return key_name, pem_path
    if existing and not pem_path.is_file():
        aws.call_text(["ec2", "delete-key-pair", "--key-name", key_name], check=False)
    created = aws.call_json(
        [
            "ec2",
            "create-key-pair",
            "--key-name",
            key_name,
            "--key-type",
            "ed25519",
            "--tag-specifications",
            tag_spec("key-pair", stack, key_name),
        ]
    )
    material = str(created.get("KeyMaterial") or "")
    if not material:
        raise AlphaError(f"ec2 create-key-pair for {key_name} returned no KeyMaterial")
    fd = os.open(str(pem_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(material)
        if not material.endswith("\n"):
            fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    return key_name, pem_path


def ensure_security_group(aws: AwsCli, stack: str, ssh_cidr: str, vpc_id: str = "") -> str:
    sg_name = f"{stack}-sg"
    filters = [f"Name=group-name,Values={sg_name}"]
    if vpc_id:
        filters.append(f"Name=vpc-id,Values={vpc_id}")
    existing = aws.call_json(["ec2", "describe-security-groups", "--filters", *filters]).get("SecurityGroups") or []
    if existing:
        sg_id = str(existing[0]["GroupId"])
    else:
        create_args = [
            "ec2",
            "create-security-group",
            "--group-name",
            sg_name,
            "--description",
            f"BranchBench alpha development worker ({stack})",
            "--tag-specifications",
            tag_spec("security-group", stack, sg_name),
        ]
        if vpc_id:
            create_args.extend(["--vpc-id", vpc_id])
        created = aws.call_json(create_args)
        sg_id = str(created.get("GroupId") or "")
        if not sg_id:
            raise AlphaError("ec2 create-security-group returned no GroupId")
    aws.call_text(
        [
            "ec2",
            "authorize-security-group-ingress",
            "--group-id",
            sg_id,
            "--protocol",
            "tcp",
            "--port",
            "22",
            "--cidr",
            ssh_cidr,
        ],
        check=False,
    )
    return sg_id


def _classify_head_bucket(aws: AwsCli, bucket: str) -> str:
    """Return 'exists' if head-bucket succeeds, 'missing' on explicit 404/NoSuchBucket, or raise AlphaError."""
    head = aws.call_text(["s3api", "head-bucket", "--bucket", bucket], check=False)
    if head.returncode == 0:
        return "exists"
    detail = (head.stderr or head.stdout or "").strip()
    low = detail.lower()
    if (
        head.returncode != 255
        and detail
        and "could not connect" not in low
        and "unable to locate credentials" not in low
        and re.search(r"\((?:404|nosuchbucket)\)|\bhttp\s+status\s+code:\s*404\b|\bnot\s+found\b", low)
    ):
        return "missing"
    raise AlphaError(f"aws s3api head-bucket failed for {bucket!r}: {detail or f'exit {head.returncode}'}")


def _inspect_bucket_ownership(aws: AwsCli, stack: str, bucket: str) -> str:
    """Return 'owned', 'foreign', or 'unknown' (when bucket tag inspection fails or cannot rule out ownership)."""
    try:
        tag_resp = aws.call_json(["s3api", "get-bucket-tagging", "--bucket", bucket])
    except AlphaError as exc:
        low = str(exc).lower()
        if "nosuchtagset" in low and not bucket.startswith(f"{stack}-"):
            return "foreign"
        return "unknown"
    tags = {
        str(item.get("Key") or ""): str(item.get("Value") or "")
        for item in (tag_resp.get("TagSet") or [])
        if isinstance(item, dict)
    }
    if (
        tags.get("Phase") == "alpha"
        and tags.get("Stack") == stack
        and tags.get("Project") in ("BranchBenchLeaderboard", "branchbench")
    ):
        return "owned"
    return "foreign"


def verify_bucket_ownership(aws: AwsCli, stack: str, bucket: str) -> None:
    """Refuse to overwrite, sync-delete, or destroy a pre-existing bucket unless its tags prove alpha stack ownership."""
    try:
        tag_resp = aws.call_json(["s3api", "get-bucket-tagging", "--bucket", bucket])
    except AlphaError as exc:
        raise AlphaError(
            f"refusing to modify or delete pre-existing S3 bucket {bucket!r}: cannot verify ownership tags ({exc})"
        ) from exc
    tags = {
        str(item.get("Key") or ""): str(item.get("Value") or "")
        for item in (tag_resp.get("TagSet") or [])
        if isinstance(item, dict)
    }
    if (
        tags.get("Phase") != "alpha"
        or tags.get("Stack") != stack
        or tags.get("Project") not in ("BranchBenchLeaderboard", "branchbench")
    ):
        raise AlphaError(
            f"refusing to modify or delete pre-existing S3 bucket {bucket!r}: "
            f"bucket tags {tags!r} do not match Phase=alpha, Stack={stack!r}"
        )


def ensure_bucket(aws: AwsCli, stack: str, bucket: str, public_site: bool = False) -> str:
    if not BUCKET_RE.fullmatch(bucket):
        raise AlphaError(f"invalid S3 bucket name {bucket!r}")
    bstate = _classify_head_bucket(aws, bucket)
    if bstate == "missing":
        create_args = ["s3api", "create-bucket", "--bucket", bucket]
        if aws.region != "us-east-1":
            create_args.extend(
                ["--create-bucket-configuration", f"LocationConstraint={aws.region}"]
            )
        aws.call_json(create_args)
    else:
        verify_bucket_ownership(aws, stack, bucket)
    tagging = (
        "TagSet=["
        "{Key=Project,Value=BranchBenchLeaderboard},"
        "{Key=Phase,Value=alpha},"
        f"{{Key=Stack,Value={stack}}}"
        "]"
    )
    aws.call_text(
        ["s3api", "put-bucket-tagging", "--bucket", bucket, "--tagging", tagging],
        check=True,
    )
    if public_site:
        aws.call_text(
            [
                "s3api",
                "put-public-access-block",
                "--bucket",
                bucket,
                "--public-access-block-configuration",
                "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false",
            ],
            check=True,
        )
        aws.call_text(["s3", "website", f"s3://{bucket}/", "--index-document", "index.html"], check=True)
        policy = json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AlphaPublicRead",
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket}/*"],
                    }
                ],
            }
        )
        aws.call_text(["s3api", "put-bucket-policy", "--bucket", bucket, "--policy", policy], check=True)
    else:
        aws.call_text(
            [
                "s3api",
                "put-public-access-block",
                "--bucket",
                bucket,
                "--public-access-block-configuration",
                "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
            ],
            check=True,
        )
    return bucket


def up_alpha(
    *,
    aws: AwsCli,
    stack: str = DEFAULT_STACK,
    instance_type: str = DEFAULT_INSTANCE_TYPE,
    ami_id: str = "",
    ssh_cidr: str = "",
    vpc_id: str = "",
    subnet_id: str = "",
    spot: bool = False,
    root_gb: int = DEFAULT_ROOT_GB,
    bucket: str = "",
    no_bucket: bool = False,
    public_site: bool = False,
    machine: str = "",
    runs_dir: Path = RUNS,
    http_getter: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    validate_stack(stack)
    machine_label = machine.strip() or default_machine_label(instance_type, aws.region)
    if not MACHINE_RE.fullmatch(machine_label):
        raise AlphaError(f"invalid --machine {machine_label!r}")
    if root_gb < 40:
        raise AlphaError("--root-gb must be at least 40 GiB")

    cidr = detect_caller_cidr(ssh_cidr, http_getter=http_getter)
    if aws.dry_run:
        plan = {
            "action": "up",
            "dry_run": True,
            "phase": "alpha",
            "stack": stack,
            "region": aws.region,
            "instance_type": instance_type,
            "spot": spot,
            "root_gb": root_gb,
            "ssh_cidr": cidr,
            "machine": machine_label,
            "bucket": None if no_bucket else (bucket or f"{stack}-<account_id>-{aws.region}"),
            "public_site": public_site,
        }
        print(json.dumps(plan, indent=2))
        return plan

    ident = caller_identity(aws)
    prev = load_state(runs_dir, stack)
    discovered = discover_stack_resources(aws, stack, prev)
    active_instances = [
        i for i in discovered["instances"] if i["state"] in ("pending", "running", "stopping", "stopped")
    ]
    if active_instances:
        existing_id = active_instances[0]["instance_id"]
        raise AlphaError(
            f"stack {stack!r} already has an EC2 instance {existing_id} ({active_instances[0]['state']}); "
            f"run './leaderboard/aws_alpha.sh status' or './leaderboard/aws_alpha.sh collapse' first"
        )

    resolved_ami = resolve_ubuntu_ami(aws, ami_id)
    state: dict[str, Any] = {
        "phase": "alpha",
        "stack": stack,
        "region": aws.region,
        "account_id": ident["account_id"],
        "caller_arn": ident["arn"],
        "instance_id": "",
        "instance_type": instance_type,
        "ami_id": resolved_ami,
        "spot": spot,
        "public_ip": "",
        "ssh_cidr": cidr,
        "security_group_id": "",
        "key_name": "",
        "key_path": "",
        "bucket": "",
        "public_site": public_site,
        "machine": machine_label,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    key_name, pem_path = ensure_key_pair(aws, stack, runs_dir)
    state["key_name"] = key_name
    state["key_path"] = str(pem_path)
    save_state(state, runs_dir, stack)

    sg_id = ensure_security_group(aws, stack, cidr, vpc_id=vpc_id)
    state["security_group_id"] = sg_id
    save_state(state, runs_dir, stack)

    bucket_name = ""
    if not no_bucket:
        target_bucket = bucket.strip() or default_bucket_name(stack, ident["account_id"], aws.region)
        state["bucket"] = target_bucket
        save_state(state, runs_dir, stack)
        try:
            bucket_name = ensure_bucket(aws, stack, target_bucket, public_site=public_site)
        except BaseException:
            if _classify_head_bucket(aws, target_bucket) == "missing":
                state["bucket"] = ""
                save_state(state, runs_dir, stack)
            raise
        state["bucket"] = bucket_name
        save_state(state, runs_dir, stack)

    runs_dir.mkdir(parents=True, exist_ok=True)
    ud_path = runs_dir / f"{stack}-userdata.sh"
    ud_path.write_text(bootstrap_user_data(), encoding="utf-8")
    try:
        bdm = json.dumps(
            [
                {
                    "DeviceName": "/dev/sda1",
                    "Ebs": {
                        "VolumeSize": root_gb,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                        "Encrypted": True,
                    },
                }
            ]
        )
        run_args = [
            "ec2",
            "run-instances",
            "--image-id",
            resolved_ami,
            "--instance-type",
            instance_type,
            "--count",
            "1",
            "--key-name",
            key_name,
            "--security-group-ids",
            sg_id,
            "--block-device-mappings",
            bdm,
            "--user-data",
            f"file://{ud_path}",
            "--tag-specifications",
            tag_spec("instance", stack),
            tag_spec("volume", stack),
        ]
        if subnet_id:
            run_args.extend(["--subnet-id", subnet_id])
        if spot:
            run_args.extend(
                [
                    "--instance-market-options",
                    "MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}",
                ]
            )
        else:
            run_args.extend(["--instance-initiated-shutdown-behavior", "terminate"])
        launched = aws.call_json(run_args)
    finally:
        ud_path.unlink(missing_ok=True)

    inst_list = launched.get("Instances") or []
    if not inst_list or not inst_list[0].get("InstanceId"):
        raise AlphaError("ec2 run-instances returned no InstanceId")
    instance_id = str(inst_list[0]["InstanceId"])
    state["instance_id"] = instance_id
    save_state(state, runs_dir, stack)

    aws.call_text(["ec2", "wait", "instance-running", "--instance-ids", instance_id])
    desc = aws.call_json(["ec2", "describe-instances", "--instance-ids", instance_id])
    inst_info = ((desc.get("Reservations") or [{}])[0].get("Instances") or [{}])[0]
    public_ip = str(inst_info.get("PublicIpAddress") or "")
    state["public_ip"] = public_ip
    save_state(state, runs_dir, stack)
    print(
        f"alpha stack {stack!r} is UP in {aws.region}: instance={instance_id} ip={public_ip} "
        f"bucket={bucket_name or '(none)'}\n"
        f"Collapse everything after testing with: ./leaderboard/aws_alpha.sh collapse --stack {stack}",
        flush=True,
    )
    return state


def deploy_alpha_site(
    *,
    aws: AwsCli,
    stack: str = DEFAULT_STACK,
    bucket: str = "",
    board: Path = BOARD,
    runs_dir: Path = RUNS,
) -> dict[str, Any]:
    validate_stack(stack)
    target = site.assemble("alpha", board)
    state = load_state(runs_dir, stack)
    target_bucket = bucket.strip() or str(state.get("bucket") or "")
    if aws.dry_run:
        plan = {
            "action": "deploy-site",
            "dry_run": True,
            "phase": "alpha",
            "stack": stack,
            "site_dir": str(target),
            "bucket": target_bucket or None,
        }
        print(json.dumps(plan, indent=2))
        return plan
    if not target_bucket:
        raise AlphaError(
            f"no alpha S3 bucket recorded for stack {stack!r}; pass --bucket <name> or run 'up' without --no-bucket"
        )
    if _classify_head_bucket(aws, target_bucket) != "exists":
        raise AlphaError(f"S3 bucket {target_bucket!r} does not exist; run 'up' first")
    verify_bucket_ownership(aws, stack, target_bucket)
    aws.call_text(
        [
            "s3",
            "sync",
            f"{target}/",
            f"s3://{target_bucket}/",
            "--delete",
        ]
    )
    presigned = aws.call_text(
        [
            "s3",
            "presign",
            f"s3://{target_bucket}/index.html",
            "--expires-in",
            "3600",
        ],
        check=False,
    )
    presigned_url = (presigned.stdout or "").strip() if presigned.returncode == 0 else ""
    website_url = (
        f"http://{target_bucket}.s3-website-{aws.region}.amazonaws.com"
        if state.get("public_site")
        else ""
    )
    result = {
        "phase": "alpha",
        "stack": stack,
        "site_dir": str(target),
        "bucket": target_bucket,
        "website_url": website_url,
        "presigned_index_url": presigned_url,
    }
    print(f"alpha site synced from {target} to s3://{target_bucket}/", flush=True)
    if website_url:
        print(f"Public alpha URL: {website_url}", flush=True)
    elif presigned_url:
        print(
            f"Presigned index.html object URL (1h, single object only; relative CSS/JS require local serve or --public-site): "
            f"{presigned_url}",
            flush=True,
        )
    print("Serve the full multi-asset alpha site locally with: python -m leaderboard.serve --phase alpha", flush=True)
    return result


def write_sync_filelist(runs_dir: Path, stack: str, root: Path = ROOT) -> Path:
    """Write an explicit allowlist of git-tracked regular files and safe git object/ref regular files (never untracked files or .git/config)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    filelist_path = runs_dir / f"{stack}-sync-files.txt"
    tracked: list[str] = []
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as exc:
        raise AlphaError(f"cannot enumerate git-tracked files in {root}: {exc}") from exc
    for line in res.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        if (
            rel == ".env"
            or rel.startswith(".env.")
            or rel.endswith((".pem", ".key"))
            or rel.startswith("leaderboard/.site/")
            or rel.startswith("leaderboard/.runs/")
        ):
            continue
        candidate = root / rel
        if candidate.is_file() and not candidate.is_symlink():
            tracked.append(rel)
    for git_file in (".git/HEAD", ".git/packed-refs"):
        gf_path = root / git_file
        if gf_path.is_file() and not gf_path.is_symlink():
            tracked.append(git_file)
    for git_dir in (".git/refs", ".git/objects"):
        gd_path = root / git_dir
        if gd_path.is_dir() and not gd_path.is_symlink():
            for child in sorted(gd_path.rglob("*")):
                if child.is_file() and not child.is_symlink() and not child.name.endswith(".tmp"):
                    tracked.append(child.relative_to(root).as_posix())
    dataset_rel = "leaderboard/data/tpcc_10w.sql"
    dataset_path = root / dataset_rel
    if dataset_path.is_file() and not dataset_path.is_symlink() and dataset_rel not in tracked:
        tracked.append(dataset_rel)
    if not tracked:
        raise AlphaError(f"git ls-files returned no syncable files in {root}")
    filelist_path.write_text("\n".join(dict.fromkeys(tracked)) + "\n", encoding="utf-8")
    return filelist_path


_GIT_CLEAN_PATHSPECS = (
    "leaderboard",
    "microbench",
    "macrobench",
    "dblib",
    "util",
    "pyproject.toml",
    "uv.lock",
    "build_protos.py",
    ":!leaderboard/*/results/*",
    ":!leaderboard/*/macro/*",
    ":!leaderboard/*/verbs/*",
    ":!leaderboard/.runs/*",
    ":!leaderboard/.site/*",
)


def reconstruct_synced_git_index(repo_dir: Path) -> None:
    """Populate .git/index from HEAD in a synced repository and verify that tracked paths are clean."""
    for git_args in (
        ["git", "-C", str(repo_dir), "read-tree", "HEAD"],
        ["git", "-C", str(repo_dir), "update-index", "-q", "--refresh"],
    ):
        res = subprocess.run(git_args, capture_output=True, text=True)
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            raise AlphaError(f"{' '.join(git_args)} failed: {detail or f'exit {res.returncode}'}")
    st = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "status",
            "--porcelain",
            "--",
            *_GIT_CLEAN_PATHSPECS,
        ],
        capture_output=True,
        text=True,
    )
    if st.returncode != 0 or st.stdout.strip():
        raise AlphaError(f"synced repository {repo_dir} is not clean after index reconstruction: {st.stdout.strip()}")


_PORTABLE_LOCK_FD = (
    'lock_fd() { if command -v flock >/dev/null 2>&1; then flock -n "$1"; '
    'else python3 -c "import fcntl, sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)" "$1" 2>/dev/null; fi; }; '
)


def _atomic_write_owner_pair(sync_token: str, lease_ttl_sec: int) -> str:
    tok = shlex.quote(sync_token)
    return (
        'now_ts="$(date +%s)" && '
        f'printf "%s:%s:%s\\n" {tok} "$now_ts" "{lease_ttl_sec}" >~/db-fork.alpha.owner.tmp && '
        "[ -s ~/db-fork.alpha.owner.tmp ] && "
        f'printf "%s:%s:%s\\n" {tok} "$now_ts" "{lease_ttl_sec}" >~/db-fork/leaderboard/.runs/alpha.owner.tmp && '
        "[ -s ~/db-fork/leaderboard/.runs/alpha.owner.tmp ] && "
        "mv -f ~/db-fork.alpha.owner.tmp ~/db-fork.alpha.owner && "
        "mv -f ~/db-fork/leaderboard/.runs/alpha.owner.tmp ~/db-fork/leaderboard/.runs/alpha.owner"
    )


def build_activate_sync_script(sync_dir: str = "~/db-fork.sync") -> str:
    """Return the shell script that validates a staged sync directory and updates ~/db-fork in-place without replacing lockfile inodes."""
    pathspecs = " ".join(shlex.quote(p) for p in _GIT_CLEAN_PATHSPECS)
    return (
        "{ "
        + _PORTABLE_LOCK_FD
        + f"git -C {sync_dir} read-tree HEAD && "
        f"git -C {sync_dir} update-index -q --refresh && "
        f'test -z "$(git -C {sync_dir} status --porcelain -- {pathspecs})" && '
        "mkdir -p ~/db-fork/leaderboard/.runs || exit 1; "
        "exec 7>>~/db-fork/leaderboard/.runs/batch.lock || exit 1; "
        "lock_fd 7 || exit 42; "
        "exec 8>>~/db-fork/leaderboard/.runs/run.lock || exit 1; "
        "lock_fd 8 || exit 42; "
        "for f in ~/db-fork/leaderboard/.runs/*.journal.json ~/db-fork/leaderboard/.runs/*.active.json; do "
        '[ -e "$f" ] && exit 43; '
        "done; "
        "rm -rf ~/db-fork.prev || exit 1; "
        "mkdir -p ~/db-fork.prev/leaderboard || exit 1; "
        "for sub in results macro verbs; do "
        "for d in ~/db-fork/leaderboard/*/$sub; do "
        '[ -d "$d" ] || continue; '
        'sys_dir="$(basename "$(dirname "$d")")"; '
        'mkdir -p ~/db-fork.prev/leaderboard/"$sys_dir" || { rm -rf ~/db-fork.prev; exit 1; }; '
        'cp -a "$d" ~/db-fork.prev/leaderboard/"$sys_dir"/ || { rm -rf ~/db-fork.prev; exit 1; }; '
        "done; "
        "done; "
        "if ! ( "
        "rsync -a --delete "
        "--exclude=/leaderboard/.runs "
        "--exclude=/leaderboard/.runs/** "
        "--exclude=/leaderboard/*/results "
        "--exclude=/leaderboard/*/results/** "
        "--exclude=/leaderboard/*/macro "
        "--exclude=/leaderboard/*/macro/** "
        "--exclude=/leaderboard/*/verbs "
        "--exclude=/leaderboard/*/verbs/** "
        "--exclude=/.venv "
        "--exclude=/.venv/** "
        f"{sync_dir}/ ~/db-fork/ && "
        "git -C ~/db-fork read-tree HEAD && "
        "git -C ~/db-fork update-index -q --refresh && "
        f'test -z "$(git -C ~/db-fork status --porcelain -- {pathspecs})" '
        "); then "
        "cp -a ~/db-fork.prev/leaderboard/. ~/db-fork/leaderboard/ 2>/dev/null || true; "
        "rm -rf ~/db-fork.prev; "
        "exit 1; "
        "fi; "
        f"rm -rf ~/db-fork.prev {sync_dir}; "
        "exec 8>&- 7>&-; "
        "}"
    )


def _is_linux_x86_64_elf(path: Path) -> bool:
    """Return True iff path is a regular file with a 64-bit little-endian x86_64 ELF header."""
    try:
        if not path.is_file() or path.is_symlink():
            return False
        with path.open("rb") as fh:
            header = fh.read(20)
    except OSError:
        return False
    if len(header) < 20:
        return False
    return (
        header[:4] == b"\x7fELF"
        and header[4] == 2  # ELFCLASS64
        and header[5] == 1  # ELFDATA2LSB
        and int.from_bytes(header[18:20], "little") == 0x3E  # EM_X86_64
    )


def run_alpha_benchmarks(
    *,
    aws: AwsCli,
    stack: str = DEFAULT_STACK,
    preset: str = "mini",
    lane: str = "all",
    systems: str = "",
    client_location: str = "",
    machine: str = "",
    batch_deadline_min: float = 720.0,
    profile_mode: str = "economic",
    deploy_site_after: bool = True,
    collapse_after: bool = False,
    board: Path = BOARD,
    runs_dir: Path = RUNS,
    cmd_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    validate_stack(stack)
    if preset not in ("mini", "full"):
        raise AlphaError(f"--preset must be mini or full, got {preset!r}")
    if lane not in ("all", "micro", "macro"):
        raise AlphaError(f"--lane must be all, micro, or macro, got {lane!r}")
    if profile_mode == "paper":
        raise AlphaError(
            "--profile-mode paper requires separated client and database VMs (paper_separated_vms); "
            "the single-VM alpha worker only supports --profile-mode economic (economic_single_vm)"
        )
    if profile_mode not in ("economic", "economic_single_vm"):
        raise AlphaError(f"--profile-mode must be economic, got {profile_mode!r}")
    state = load_state(runs_dir, stack)
    public_ip = str(state.get("public_ip") or "")
    key_path = str(state.get("key_path") or key_file_path(runs_dir, stack))
    machine_label = machine.strip() or str(state.get("machine") or default_machine_label(DEFAULT_INSTANCE_TYPE, aws.region))
    if not MACHINE_RE.fullmatch(machine_label):
        raise AlphaError(f"invalid --machine {machine_label!r}")

    worker_cmd = [
        "./leaderboard/aws_worker.sh",
        "--machine",
        machine_label,
        "--preset",
        preset,
        "--data-mount",
        "/mnt/nvme/branchbench",
        "--batch-deadline-min",
        f"{batch_deadline_min:g}",
    ]
    if lane in ("micro", "macro"):
        worker_cmd.extend(["--lane", lane])
    if systems.strip():
        worker_cmd.extend(["--systems", systems.strip()])
    if client_location.strip():
        worker_cmd.extend(["--client-location", client_location.strip()])

    if aws.dry_run:
        plan = {
            "action": "run",
            "dry_run": True,
            "phase": "alpha",
            "stack": stack,
            "profile_mode": profile_mode,
            "public_ip": public_ip or "<from-state>",
            "remote_cmd": shlex.join(worker_cmd),
            "deploy_site_after": deploy_site_after,
            "collapse_after": collapse_after,
        }
        print(json.dumps(plan, indent=2))
        return 0

    if not public_ip:
        raise AlphaError(f"no running alpha instance public_ip in state for stack {stack!r}; run 'up' first")
    if not Path(key_path).is_file():
        raise AlphaError(f"SSH key {key_path} not found; run 'up' or 'collapse' first")

    ssh_opts = [
        "-i",
        key_path,
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=15",
    ]
    ssh_target = f"ubuntu@{public_ip}"
    ssh_rsh = f"ssh {' '.join(shlex.quote(o) for o in ssh_opts)}"
    collapse_failed = False
    collapse_armed = False
    lease_acquired = False
    lease_ttl_sec = max(86400, int(batch_deadline_min * 60) + 7200)
    sync_token = f"{os.getpid()}_{int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)}"
    sync_dir = f"~/db-fork.sync.{sync_token}"

    try:
        # 1. Wait for cloud-init bootstrap marker on the worker, ensure no lock or recovery journal is active, claim sync lease, and prepare staging dir
        wait_script = (
            "{ "
            + _PORTABLE_LOCK_FD
            + "for i in $(seq 1 90); do "
            "if [ -f /var/lib/branchbench-alpha-ready ]; then "
            "mkdir -p ~/db-fork/leaderboard/.runs || exit 1; "
            "exec 9>>~/db-fork/leaderboard/.runs/alpha.lock || exit 1; "
            "lock_fd 9 || exit 42; "
            "exec 7>>~/db-fork/leaderboard/.runs/batch.lock || exit 1; "
            "lock_fd 7 || exit 42; "
            "exec 8>>~/db-fork/leaderboard/.runs/run.lock || exit 1; "
            "lock_fd 8 || exit 42; "
            "for owner_file in ~/db-fork.alpha.owner ~/db-fork/leaderboard/.runs/alpha.owner; do "
            'if [ -f "$owner_file" ]; then '
            'owner_raw="$(cat "$owner_file" 2>/dev/null)" || exit 42; '
            "case \"$owner_raw\" in ''|*[[:space:]]*) exit 42 ;; esac; "
            'colon_cnt="$(printf "%s" "$owner_raw" | tr -cd ":" | wc -c | tr -d " ")"; '
            'owner_tok="$(printf "%s" "$owner_raw" | cut -d: -f1)"; '
            'owner_ts="$(printf "%s" "$owner_raw" | cut -d: -f2)"; '
            '[ -n "$owner_tok" ] || exit 42; '
            "case \"$owner_ts\" in ''|*[!0-9]*) exit 42 ;; esac; "
            '[ "$owner_ts" -gt 0 ] || exit 42; '
            'if [ "$colon_cnt" -eq 1 ]; then owner_ttl=86400; '
            'elif [ "$colon_cnt" -eq 2 ]; then '
            'owner_ttl="$(printf "%s" "$owner_raw" | cut -d: -f3)"; '
            "case \"$owner_ttl\" in ''|*[!0-9]*) exit 42 ;; esac; "
            '[ "$owner_ttl" -gt 0 ] || exit 42; '
            "else exit 42; fi; "
            'now_ts="$(date +%s)"; '
            'if [ "$((now_ts - owner_ts))" -lt "$owner_ttl" ]; then exit 42; fi; '
            "fi; "
            "done; "
            "for f in ~/db-fork/leaderboard/.runs/*.journal.json ~/db-fork/leaderboard/.runs/*.active.json; do "
            '[ -e "$f" ] && exit 43; '
            "done; "
            f"{{ {_atomic_write_owner_pair(sync_token, lease_ttl_sec)} || {{ rm -f ~/db-fork.alpha.owner.tmp ~/db-fork/leaderboard/.runs/alpha.owner.tmp; exit 1; }}; }} && "
            f"rm -rf {sync_dir} && mkdir -p {sync_dir} && exit 0; "
            "fi; "
            "sleep 5; "
            "done; exit 1; "
            "}"
        )
        wait_cmd = [
            "ssh",
            *ssh_opts,
            "-o",
            "ConnectionAttempts=10",
            ssh_target,
            wait_script,
        ]
        wait_res = cmd_runner(wait_cmd)
        if wait_res.returncode == 42:
            raise AlphaError(f"remote worker {ssh_target} has an active benchmark lock in ~/db-fork/leaderboard/.runs")
        if wait_res.returncode == 43:
            raise AlphaError(
                f"remote worker {ssh_target} has an unrecovered journal or active run in ~/db-fork/leaderboard/.runs"
            )
        if wait_res.returncode != 0:
            raise AlphaError(
                f"timed out waiting for cloud-init bootstrap (/var/lib/branchbench-alpha-ready) on {ssh_target}"
            )
        lease_acquired = True
        collapse_armed = True

        # 2. Sync only git-tracked regular files and safe .git metadata into per-invocation staging dir (portable across GNU rsync and macOS openrsync)
        filelist_path = write_sync_filelist(runs_dir, stack, ROOT)
        try:
            sync_up = [
                "rsync",
                "-az",
                f"--files-from={filelist_path}",
                "-e",
                ssh_rsh,
                f"{ROOT}/",
                f"{ssh_target}:{sync_dir}/",
            ]
            if cmd_runner(sync_up).returncode != 0:
                raise AlphaError(f"failed to rsync repository to {ssh_target}:{sync_dir}/")
        finally:
            filelist_path.unlink(missing_ok=True)

        # 2b. If local ~/mo/matrixone or ~/seekdb has a Linux x86_64 ELF binary on the operator machine, sync binaries only (never data/log dirs)
        selected_tokens = set(systems.replace(",", " ").split()) if systems.strip() else {"matrixone", "seekdb"}
        mo_local = Path.home() / "mo" / "matrixone"
        if "matrixone" in selected_tokens and _is_linux_x86_64_elf(mo_local / "mo-service"):
            mo_sync = [
                "rsync",
                "-az",
                "--exclude=mo-data",
                "--exclude=mo-data/**",
                "--exclude=store",
                "--exclude=store/**",
                "--exclude=log",
                "--exclude=log/**",
                "--exclude=*.log",
                "--exclude=*.pid",
                "-e",
                ssh_rsh,
                f"{mo_local}/",
                f"{ssh_target}:~/mo/matrixone/",
            ]
            if cmd_runner(mo_sync).returncode != 0:
                raise AlphaError(f"failed to rsync MatrixOne binary from {mo_local} to {ssh_target}")
        seekdb_local = Path.home() / "seekdb"
        if "seekdb" in selected_tokens and _is_linux_x86_64_elf(seekdb_local / "bin" / "seekdb"):
            seekdb_sync = [
                "rsync",
                "-az",
                "--exclude=store",
                "--exclude=store/**",
                "--exclude=log",
                "--exclude=log/**",
                "--exclude=*.log",
                "--exclude=*.pid",
                "-e",
                ssh_rsh,
                f"{seekdb_local}/",
                f"{ssh_target}:~/seekdb/",
            ]
            if cmd_runner(seekdb_sync).returncode != 0:
                raise AlphaError(f"failed to rsync SeekDB binary from {seekdb_local} to {ssh_target}")

        # 3. Reconstruct .git/index in staging dir, preserve .runs and existing results/macro/verbs, swap into ~/db-fork, and execute worker under lock
        env_stdin = "".join(
            f"export {k}={shlex.quote(os.environ[k])}\n"
            for k in HOSTED_ENV_KEYS
            for val in (os.environ.get(k),)
            if val
        )
        deadman_min = max(15, int(batch_deadline_min) + 15)
        activate_sync = build_activate_sync_script(sync_dir)
        remote_steps = [
            'export PATH="$HOME/.local/bin:$PATH"',
            f"export DBFORK_ALPHA_TOKEN={shlex.quote(sync_token)}",
            "{ "
            + _PORTABLE_LOCK_FD
            + "mkdir -p ~/db-fork/leaderboard/.runs || exit 1; "
            + "exec 9>>~/db-fork/leaderboard/.runs/alpha.lock || exit 1; "
            + "lock_fd 9 || exit 42; "
            + f"grep -q '^{sync_token}:' ~/db-fork.alpha.owner 2>/dev/null || exit 42; "
            + "}",
            (
                f"trap 'ec=$?; rm -rf {sync_dir}; "
                f'if grep -q "^{sync_token}:" ~/db-fork.alpha.owner 2>/dev/null; then '
                f"if ! {{ {_atomic_write_owner_pair(sync_token, lease_ttl_sec)}; }}; then "
                "rm -f ~/db-fork.alpha.owner.tmp ~/db-fork/leaderboard/.runs/alpha.owner.tmp; exit 1; "
                "fi; "
                'fi; exit "$ec"\' EXIT'
            ),
            f"sudo -n /sbin/shutdown -P +{deadman_min} >/dev/null 2>&1",
            *(["eval \"$(cat)\""] if env_stdin else []),
            activate_sync,
            "cd ~/db-fork",
            "(uv sync --frozen --extra chronos || uv sync --frozen)",
            "uv run --frozen python build_protos.py",
            shlex.join(worker_cmd),
        ]
        remote_script = " && ".join(remote_steps)
        ssh_run_cmd = ["ssh", *ssh_opts, ssh_target, f"bash -lc {shlex.quote(remote_script)}"]
        if env_stdin:
            run_res = cmd_runner(ssh_run_cmd, input=env_stdin, text=True)
        else:
            run_res = cmd_runner(ssh_run_cmd)
        if run_res.returncode in (42, 43):
            collapse_armed = False
            raise AlphaError(
                f"remote worker {ssh_target} rejected activation due to an active benchmark lock or recovery journal (exit {run_res.returncode})"
            )

        # 4. Pull results, macro, and verbs records back to local leaderboard/ even if a benchmark step failed
        pull_cmd = [
            "rsync",
            "-az",
            "--include=*/",
            "--include=*/results/***",
            "--include=*/macro/***",
            "--include=*/verbs/***",
            "--exclude=*",
            "-e",
            ssh_rsh,
            f"{ssh_target}:~/db-fork/leaderboard/",
            f"{board}/",
        ]
        if cmd_runner(pull_cmd).returncode != 0:
            raise AlphaError(f"failed to pull results from {ssh_target}")

        # 5. Build local leaderboard/.site/alpha
        site.assemble("alpha", board)
        if deploy_site_after and state.get("bucket"):
            deploy_alpha_site(aws=aws, stack=stack, board=board, runs_dir=runs_dir)

        rc = run_res.returncode
    finally:
        release_rc = 0
        if lease_acquired:
            will_collapse = bool(collapse_after and collapse_armed)
            owner_post_action = (
                f"if {{ {_atomic_write_owner_pair(sync_token, lease_ttl_sec)}; }}; then exit 0; "
                "else rm -f ~/db-fork.alpha.owner.tmp ~/db-fork/leaderboard/.runs/alpha.owner.tmp; exit 1; fi; "
                if will_collapse
                else "rm -f ~/db-fork.alpha.owner ~/db-fork/leaderboard/.runs/alpha.owner && exit 0 || exit 1; "
            )
            release_script = (
                "{ "
                + _PORTABLE_LOCK_FD
                + "mkdir -p ~/db-fork/leaderboard/.runs || exit 1; "
                "exec 9>>~/db-fork/leaderboard/.runs/alpha.lock || exit 1; "
                "lock_fd 9 || exit 42; "
                f"rm -rf {sync_dir}; "
                f"if grep -q '^{sync_token}:' ~/db-fork.alpha.owner 2>/dev/null && "
                f"{{ [ ! -f ~/db-fork/leaderboard/.runs/alpha.owner ] || grep -q '^{sync_token}:' ~/db-fork/leaderboard/.runs/alpha.owner 2>/dev/null; }}; then "
                + owner_post_action
                + "fi; exit 42; "
                "}"
            )
            rel_res = cmd_runner(["ssh", *ssh_opts, ssh_target, release_script])
            if rel_res.returncode != 0:
                release_rc = rel_res.returncode or 1
                collapse_failed = True
                print(
                    f"error: failed to verify/release alpha lease on {ssh_target} (exit {rel_res.returncode})",
                    file=sys.stderr,
                )
        if collapse_after and collapse_armed and release_rc == 0:
            col_res = collapse_alpha(aws=aws, stack=stack, board=board, runs_dir=runs_dir, keep_local_site=True)
            if not col_res.get("collapsed", True):
                collapse_failed = True
    if release_rc != 0 and rc == 0:
        return release_rc
    return 1 if collapse_failed and rc == 0 else rc


def status_alpha(
    *,
    aws: AwsCli,
    stack: str = DEFAULT_STACK,
    board: Path = BOARD,
    runs_dir: Path = RUNS,
) -> dict[str, Any]:
    validate_stack(stack)
    state = load_state(runs_dir, stack)
    alpha_site = board / ".site" / "alpha"
    if aws.dry_run:
        info = {
            "phase": "alpha",
            "stack": stack,
            "dry_run": True,
            "state_file_exists": state_file_path(runs_dir, stack).is_file(),
            "local_alpha_site_exists": alpha_site.is_dir(),
            "recorded_state": state,
        }
        print(json.dumps(info, indent=2))
        return info

    discovered = discover_stack_resources(aws, stack, state)
    candidate_buckets = discovered.get("buckets") or ([discovered["bucket"]] if discovered.get("bucket") else [])
    existing_buckets = [b for b in candidate_buckets if _classify_head_bucket(aws, b) == "exists"]
    unverified_buckets = list(discovered.get("unverified_buckets") or [])
    bucket_discovery_error = str(discovered.get("bucket_discovery_error") or "")
    bucket_exists = bool(existing_buckets or unverified_buckets or bucket_discovery_error)
    active_compute = any(
        i["state"] in ("pending", "running", "stopping", "stopped") for i in discovered["instances"]
    )
    report = {
        "phase": "alpha",
        "stack": stack,
        "region": aws.region,
        "instances": discovered["instances"],
        "volumes": discovered["volumes"],
        "security_groups": discovered["security_groups"],
        "key_pairs": discovered["key_pairs"],
        "bucket": existing_buckets[0] if existing_buckets else None,
        "buckets": existing_buckets,
        "unverified_buckets": unverified_buckets,
        "bucket_discovery_error": bucket_discovery_error or None,
        "local_alpha_site": str(alpha_site) if alpha_site.is_dir() else None,
        "state_file": str(state_file_path(runs_dir, stack)) if state_file_path(runs_dir, stack).is_file() else None,
        "collapsed": not (
            active_compute
            or discovered["volumes"]
            or discovered["security_groups"]
            or discovered["key_pairs"]
            or bucket_exists
        ),
    }
    print(json.dumps(report, indent=2), flush=True)
    return report


def _empty_and_delete_bucket(aws: AwsCli, bucket: str, stack: str = DEFAULT_STACK) -> bool:
    try:
        bstate = _classify_head_bucket(aws, bucket)
    except AlphaError:
        return False
    if bstate == "missing":
        return True
    try:
        verify_bucket_ownership(aws, stack, bucket)
    except AlphaError:
        return False
    rm_res = aws.call_text(["s3", "rm", f"s3://{bucket}", "--recursive"], check=False)
    if rm_res.returncode != 0:
        return False
    for _ in range(10):
        try:
            versions = aws.call_json(["s3api", "list-object-versions", "--bucket", bucket])
        except AlphaError:
            return False
        objs = [
            {"Key": str(item["Key"]), "VersionId": str(item["VersionId"])}
            for key in ("Versions", "DeleteMarkers")
            for item in (versions.get(key) or [])
            if item.get("Key") and item.get("VersionId")
        ]
        if not objs:
            break
        for idx in range(0, len(objs), 500):
            batch = json.dumps({"Objects": objs[idx : idx + 500], "Quiet": True})
            del_obj = aws.call_text(
                ["s3api", "delete-objects", "--bucket", bucket, "--delete", batch],
                check=False,
            )
            if del_obj.returncode != 0:
                return False
        if not versions.get("IsTruncated"):
            break
    del_res = aws.call_text(["s3api", "delete-bucket", "--bucket", bucket], check=False)
    return del_res.returncode == 0


def collapse_alpha(
    *,
    aws: AwsCli,
    stack: str = DEFAULT_STACK,
    keep_bucket: bool = False,
    keep_local_site: bool = False,
    board: Path = BOARD,
    runs_dir: Path = RUNS,
) -> dict[str, Any]:
    """Tear down every AWS and local resource created by the alpha phase for <stack>."""
    validate_stack(stack)
    state = load_state(runs_dir, stack)
    spath = state_file_path(runs_dir, stack)
    kpath = key_file_path(runs_dir, stack)
    alpha_site = board / ".site" / "alpha"

    if aws.dry_run:
        plan = {
            "action": "collapse",
            "dry_run": True,
            "phase": "alpha",
            "stack": stack,
            "region": aws.region,
            "terminate_instance_id": state.get("instance_id"),
            "delete_security_group_id": state.get("security_group_id"),
            "delete_key_name": state.get("key_name") or f"{stack}-key",
            "delete_bucket": None if keep_bucket else state.get("bucket"),
            "remove_local_alpha_site": not keep_local_site,
            "remove_state_file": str(spath),
        }
        print(json.dumps(plan, indent=2))
        return plan

    discovered = discover_stack_resources(aws, stack, state)
    instance_ids = [i["instance_id"] for i in discovered["instances"]]
    if instance_ids:
        aws.call_text(["ec2", "terminate-instances", "--instance-ids", *instance_ids])
        aws.call_text(["ec2", "wait", "instance-terminated", "--instance-ids", *instance_ids])

    # Refresh volumes after instance termination in case any tagged volume remained available
    vol_resp = aws.call_json(
        [
            "ec2",
            "describe-volumes",
            "--filters",
            "Name=tag:Project,Values=BranchBenchLeaderboard",
            "Name=tag:Phase,Values=alpha",
            f"Name=tag:Stack,Values={stack}",
        ]
    )
    volume_ids = [str(v["VolumeId"]) for v in (vol_resp.get("Volumes") or []) if v.get("VolumeId")]
    deleted_volumes: list[str] = []
    remaining_volumes: list[str] = []
    for vid in volume_ids:
        if aws.call_text(["ec2", "delete-volume", "--volume-id", vid], check=False).returncode == 0:
            deleted_volumes.append(vid)
        else:
            remaining_volumes.append(vid)

    deleted_sgs: list[str] = []
    remaining_sgs: list[str] = []
    for sg_id in discovered["security_groups"]:
        if aws.call_text(["ec2", "delete-security-group", "--group-id", sg_id], check=False).returncode == 0:
            deleted_sgs.append(sg_id)
        else:
            remaining_sgs.append(sg_id)

    key_names = list(dict.fromkeys([*discovered["key_pairs"], f"{stack}-key"]))
    deleted_keys: list[str] = []
    remaining_keys: list[str] = []
    for kname in key_names:
        if aws.call_text(["ec2", "delete-key-pair", "--key-name", kname], check=False).returncode == 0:
            deleted_keys.append(kname)
        else:
            remaining_keys.append(kname)

    candidate_buckets = discovered.get("buckets") or ([discovered["bucket"]] if discovered.get("bucket") else [])
    unverified_buckets = list(discovered.get("unverified_buckets") or [])
    bucket_discovery_error = str(discovered.get("bucket_discovery_error") or "")
    deleted_buckets: list[str] = []
    remaining_buckets: list[str] = list(candidate_buckets) if keep_bucket else []
    if not keep_bucket:
        for b_name in candidate_buckets:
            if _empty_and_delete_bucket(aws, b_name, stack=stack):
                deleted_buckets.append(b_name)
            else:
                remaining_buckets.append(b_name)
        for ub in unverified_buckets:
            if ub not in remaining_buckets:
                remaining_buckets.append(ub)
    deleted_bucket = deleted_buckets[0] if deleted_buckets else None
    remaining_bucket = remaining_buckets[0] if remaining_buckets else ""

    if not remaining_keys:
        kpath.unlink(missing_ok=True)
        if state.get("key_path"):
            Path(str(state["key_path"])).unlink(missing_ok=True)

    has_remaining = bool(
        remaining_volumes
        or remaining_sgs
        or remaining_keys
        or remaining_buckets
        or (not keep_bucket and bucket_discovery_error)
    )
    if has_remaining:
        updated = dict(state)
        updated["instance_id"] = ""
        updated["public_ip"] = ""
        updated["security_group_id"] = remaining_sgs[0] if remaining_sgs else ""
        updated["key_name"] = remaining_keys[0] if remaining_keys else ""
        updated["bucket"] = remaining_bucket
        save_state(updated, runs_dir, stack)
    else:
        spath.unlink(missing_ok=True)

    if not keep_local_site and alpha_site.is_dir() and not alpha_site.is_symlink():
        shutil.rmtree(alpha_site)

    summary = {
        "action": "collapse",
        "phase": "alpha",
        "stack": stack,
        "region": aws.region,
        "collapsed": not has_remaining,
        "terminated_instances": instance_ids,
        "deleted_volumes": deleted_volumes,
        "deleted_security_groups": deleted_sgs,
        "deleted_key_pairs": deleted_keys,
        "deleted_bucket": deleted_bucket,
        "deleted_buckets": deleted_buckets,
        "remaining_volumes": remaining_volumes,
        "remaining_security_groups": remaining_sgs,
        "remaining_key_pairs": remaining_keys,
        "remaining_bucket": remaining_bucket or None,
        "remaining_buckets": remaining_buckets,
        "bucket_discovery_error": bucket_discovery_error or None,
        "local_alpha_site_removed": not keep_local_site and not alpha_site.exists(),
        "state_file_removed": not spath.exists(),
        "ongoing_aws_cost": "$0.00/hr" if not has_remaining else "partial (see remaining resources)",
    }
    print(
        f"alpha stack {stack!r} collapsed in {aws.region}: "
        f"terminated {len(instance_ids)} instance(s), deleted {len(deleted_sgs)} SG(s), "
        f"bucket={deleted_bucket or '(kept/none)'}, ongoing AWS cost={summary['ongoing_aws_cost']}",
        flush=True,
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aws_alpha",
        description="Provision, run, deploy, inspect, and collapse the personal-AWS 'alpha' leaderboard phase.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--stack", default=DEFAULT_STACK, help=f"Alpha stack name (default: {DEFAULT_STACK})")
    common.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION,
        help=f"AWS region (default: $AWS_REGION or {DEFAULT_REGION})",
    )
    common.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""), help="Optional AWS CLI profile name")
    common.add_argument("--dry-run", action="store_true", help="Print the planned actions as JSON without calling AWS")

    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", parents=[common], help="Provision the collapsible alpha EC2 worker and S3 bucket")
    up.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE, help=f"EC2 instance type (default: {DEFAULT_INSTANCE_TYPE})")
    up.add_argument("--ami-id", default="", help="Explicit Ubuntu 24.04 x86_64 AMI ID (default: auto-resolve via SSM)")
    up.add_argument("--ssh-cidr", default="", help="Allowed inbound SSH IPv4 CIDR (default: auto-detect caller /32)")
    up.add_argument("--vpc-id", default="", help="Optional VPC ID for the security group")
    up.add_argument("--subnet-id", default="", help="Optional subnet ID for the EC2 instance")
    up.add_argument("--spot", action="store_true", help="Launch as a one-time EC2 Spot instance (~60-70%% cheaper)")
    up.add_argument("--root-gb", type=int, default=DEFAULT_ROOT_GB, help=f"Root gp3 EBS size in GiB (default: {DEFAULT_ROOT_GB})")
    up.add_argument("--bucket", default="", help="Explicit S3 bucket name for hosting .site/alpha")
    up.add_argument("--no-bucket", action="store_true", help="Do not create an S3 bucket (compute worker only)")
    up.add_argument("--public-site", action="store_true", help="Configure the alpha S3 bucket for public static website hosting")
    up.add_argument("--machine", default="", help="Machine label recorded in benchmark results")

    run_p = sub.add_parser("run", parents=[common], help="Sync repo to alpha EC2 worker, run benchmarks, pull results, and build .site/alpha")
    run_p.add_argument("--preset", choices=("mini", "full"), default="mini", help="Macrobench preset (default: mini for alpha testing)")
    run_p.add_argument("--lane", choices=("all", "micro", "macro"), default="all", help="Benchmark lanes to run (default: all)")
    run_p.add_argument("--systems", default="", help="Space- or comma-separated systems to benchmark")
    run_p.add_argument("--client-location", default="", help="Client location label when benchmarking hosted systems")
    run_p.add_argument("--machine", default="", help="Override machine label")
    run_p.add_argument("--batch-deadline-min", type=float, default=720.0, help="Batch wall-clock timeout in minutes")
    run_p.add_argument("--profile-mode", choices=("economic", "paper"), default="economic", help="Execution profile: 'economic' (default 1-VM sequential) or 'paper'")
    run_p.add_argument("--no-deploy-site", action="store_true", help="Build .site/alpha locally without syncing to S3")
    run_p.add_argument("--collapse-after", action="store_true", help="Automatically collapse all AWS resources after pulling results")

    dep = sub.add_parser("deploy-site", parents=[common], help="Assemble leaderboard/.site/alpha and sync to the alpha S3 bucket")
    dep.add_argument("--bucket", default="", help="Override target S3 bucket name")

    sub.add_parser("status", parents=[common], help="Inspect active AWS resources and local build state for the alpha phase")

    for alias in ("collapse", "down", "destroy", "teardown"):
        col = sub.add_parser(
            alias,
            parents=[common],
            help="Completely tear down the alpha EC2 worker, volumes, SG, key pair, S3 bucket, and local alpha state",
        )
        col.add_argument("--keep-bucket", action="store_true", help="Keep the alpha S3 bucket while collapsing all compute/network resources")
        col.add_argument("--keep-local-site", action="store_true", help="Keep leaderboard/.site/alpha on local disk")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    aws = AwsCli(region=args.region, profile=args.profile, dry_run=args.dry_run)
    try:
        if args.command == "up":
            up_alpha(
                aws=aws,
                stack=args.stack,
                instance_type=args.instance_type,
                ami_id=args.ami_id,
                ssh_cidr=args.ssh_cidr,
                vpc_id=args.vpc_id,
                subnet_id=args.subnet_id,
                spot=args.spot,
                root_gb=args.root_gb,
                bucket=args.bucket,
                no_bucket=args.no_bucket,
                public_site=args.public_site,
                machine=args.machine,
            )
            return 0
        if args.command == "run":
            return run_alpha_benchmarks(
                aws=aws,
                stack=args.stack,
                preset=args.preset,
                lane=args.lane,
                systems=args.systems,
                client_location=args.client_location,
                machine=args.machine,
                batch_deadline_min=args.batch_deadline_min,
                profile_mode=args.profile_mode,
                deploy_site_after=not args.no_deploy_site,
                collapse_after=args.collapse_after,
            )
        if args.command == "deploy-site":
            deploy_alpha_site(aws=aws, stack=args.stack, bucket=args.bucket)
            return 0
        if args.command == "status":
            status_alpha(aws=aws, stack=args.stack)
            return 0
        if args.command in ("collapse", "down", "destroy", "teardown"):
            res = collapse_alpha(
                aws=aws,
                stack=args.stack,
                keep_bucket=args.keep_bucket,
                keep_local_site=args.keep_local_site,
            )
            return 0 if res.get("collapsed", True) else 1
    except AlphaError as exc:
        print(f"aws_alpha: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
