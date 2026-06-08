# Nagios Server Container for Rocky Linux 9.6

## Overview

This project provides a containerized Nagios monitoring server based on Rocky Linux 9.6.

The container includes:

* Nagios Core
* Nagios Plugins
* NRPE
* Apache HTTPD
* Supervisor
* Python3
* Dynamic host generation scripts
* HPC Cluster Monitoring Support

The solution is designed for monitoring:

* Master Nodes
* Management Nodes
* Login Nodes
* Compute Nodes
* GPU Nodes
* High Memory Nodes

---

# Architecture

+--------------------------------------------------------------------------------+
|                           Nagios Monitoring Container                          |
+--------------------------------------------------------------------------------+
|                                Supervisord                                     |
+--------------------------------------------------------------------------------+
|                                                                            |
|     +-----------+      +-----------+      +-----------+                   |
|     |   HTTPD   |      |  NAGIOS   |      |   NRPE    |                   |
|     +-----------+      +-----------+      +-----------+                   |
|                                                                            |
+--------------------------------------------------------------------------------+
                                       |
                                       |
                                       v
+--------------------------------------------------------------------------------+
|                              HPC Cluster Nodes                                |
+--------------------------------------------------------------------------------+
|                                                                               |
|   Master   |   Management   |   Login   |   Compute   |   GPU   |   HM       |
|                                                                               |
+--------------------------------------------------------------------------------+



---

# Build Docker Image

Clone repository:

```bash
git clone <repository_url>

cd nagios-rocky9.6
```

Build image:

```bash
docker build \
    -t cdac_nagios/rocky9.6:latest \
    .
```

Verify:

```bash
docker images
```

Expected:

```text
REPOSITORY              TAG       IMAGE ID
cdac_nagios/rocky9.6    latest    xxxxxxxxxxxx
```

---

# Create Required Directories

```bash
mkdir -p /hpctool_stack/nagios

mkdir -p /hpctool_stack/nagios/nagiosdata

mkdir -p /hpctool_stack/nagios/conf

mkdir -p /hpctool_stack/nagios/log
```

---

# Docker Compose Deployment

Create:

```bash
vi docker-compose.yml
```

```yaml
version: "3.8"

services:

  nagios:

    image: cdac_nagios/rocky9.6:latest

    container_name: nagios

    network_mode: host

    restart: unless-stopped

    environment:

      PORT: 8080

      ROOT_PASSWD: root123

      NAGIOS_USER: nagiosadmin

      NAGIOS_USER_PASSWD: nagios123

      Master_IP: 192.168.1.10

      Host_IP: 192.168.1.10

      compute_g_name: compute

      hm_g_name: hm

      gpu_g_name: gpu

      master_g_name: master

      login_g_name: login

      management_g_name: mgmt

    volumes:

      - /hpctool_stack/nagios/nagiosdata:/nagiosdata

      - /hpctool_stack/nagios/log:/var/log/nagios

      - /hpctool_stack/nagios/conf:/etc/nagios/conf.d
```

Start container:

```bash
docker compose up -d
```

Verify:

```bash
docker ps
```

---

# Access Nagios Web Interface

Using Host Networking:

```text
http://<server-ip>/nagios
```

Example:

```text
http://192.168.1.10/nagios
```

Login:

```text
Username : nagiosadmin
Password : <configured password>
```

---

# Configure Cluster Nodes

Enter container:

```bash
docker exec -it nagios bash
```

Go to configuration directory:

```bash
cd /nagios_conf
```

Generate node definitions:

```bash
./hosts.cfg_add.sh
```

Example:

```text
Enter Template Name: PARAM RUDRA

Add MASTER nodes? (y/n): y

Add MANAGEMENT nodes? (y/n): y

Add LOGIN nodes? (y/n): y

Add COMPUTE nodes? (y/n): y

Add HIGH MEMORY nodes? (y/n): n

Add GPU nodes? (y/n): y
```

The script automatically:

* Creates hosts.cfg
* Creates services.cfg
* Generates node definitions
* Validates Nagios configuration

---

# Validate Configuration

```bash
nagios -v /etc/nagios/nagios.cfg
```

Expected:

```text
Things look okay - No serious problems were detected
```

---

# Reload Nagios

```bash
supervisorctl restart nagios
```

or

```bash
/etc/init.d/nagios restart
```

---

# Supervisor Management

Check status:

```bash
supervisorctl status
```

Example:

```text
httpd      RUNNING
nagios     RUNNING
nrpe       RUNNING
```

---

## Restart Nagios

```bash
supervisorctl restart nagios
```

---

## Restart Apache

```bash
supervisorctl restart httpd
```

---

## Restart NRPE

```bash
supervisorctl restart nrpe
```

---

## Restart All Services

```bash
supervisorctl restart all
```

---

## Stop Nagios

```bash
supervisorctl stop nagios
```

---

## Start Nagios

```bash
supervisorctl start nagios
```

---

# View Logs

Nagios:

```bash
supervisorctl tail nagios
```

Apache:

```bash
supervisorctl tail httpd
```

NRPE:

```bash
supervisorctl tail nrpe
```

Supervisor:

```bash
cat /var/log/supervisor/supervisord.log
```

---

# Container Health Verification

Verify processes:

```bash
ps -ef | egrep "nagios|httpd|nrpe"
```

Verify web server:

```bash
curl http://localhost/nagios
```

Verify Nagios configuration:

```bash
nagios -v /etc/nagios/nagios.cfg
```

---

# Persistent Data

The following directories are persistent:

| Host Directory                   | Container Directory |
| -------------------------------- | ------------------- |
| /hpctool_stack/nagios/conf       | /etc/nagios/conf.d  |
| /hpctool_stack/nagios/log        | /var/log/nagios     |
| /hpctool_stack/nagios/nagiosdata | /nagiosdata         |

Container upgrades will not remove monitoring configuration.

---

# Troubleshooting

Validate configuration:

```bash
nagios -v /etc/nagios/nagios.cfg
```

Check supervisor:

```bash
supervisorctl status
```

Check Apache:

```bash
curl http://localhost/nagios
```

Check running processes:

```bash
ps -ef
```

Check container logs:

```bash
docker logs nagios
```

---

# License

This project is intended for HPC cluster monitoring environments and can be customized for enterprise infrastructure monitoring deployments.
