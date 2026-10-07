#!/bin/bash
set -eu
export DEBIAN_FRONTEND=noninteractive
# The EIP is associated after instance creation; package bootstrap can start first.
for attempt in $(seq 1 30); do
  if apt-get update; then break; fi
  sleep 10
done
apt-get install -y python3.12-venv postgresql postgresql-client curl unzip make git
python3 -m venv /opt/s1-tools
/opt/s1-tools/bin/pip install uv==0.12.5
ln -sf /opt/s1-tools/bin/uv /usr/local/bin/uv
curl -fsS https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp/s1-awscli
/tmp/s1-awscli/aws/install
rm -rf /tmp/s1-awscli /tmp/awscli.zip
snap list amazon-ssm-agent >/dev/null 2>&1 || snap install amazon-ssm-agent --classic
snap start amazon-ssm-agent
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.aws
cat > /home/ubuntu/.aws/config <<'EOF'
[profile agent-runtime]
region = us-east-2
credential_source = Ec2InstanceMetadata
EOF
chown ubuntu:ubuntu /home/ubuntu/.aws/config
chmod 600 /home/ubuntu/.aws/config
# PostgreSQL's server tools are not in Ubuntu's default PATH; tests create isolated DBs.
for tool in initdb pg_ctl; do
  ln -sf "/usr/lib/postgresql/16/bin/$tool" "/usr/local/bin/$tool"
done
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/s1
printf 'ready\n' > /var/lib/s1-ready
