#!/bin/bash

set -Eeuo pipefail

###############################################################################
# Logging
###############################################################################

log() {
    echo "[INFO ] $(date '+%F %T') $*"
}

error() {
    echo "[ERROR] $(date '+%F %T') $*" >&2
    exit 1
}

###############################################################################
# Already Configured ?
###############################################################################

if [[ -f /root/CONFIGURED ]] || [[ -f /etc/nagios/CONFIGURED ]]; then

    log "Container already configured."

    #
    # Restore original nagiosdata if required
    #
    if [[ -d /nagiosdata.NEEDINIT ]]; then

        rm -rf /nagiosdata.orig 2>/dev/null || true

        mv /nagiosdata.NEEDINIT \
           /nagiosdata.orig 2>/dev/null || true
    fi

    log "Starting supervisord..."

    exec /usr/bin/supervisord \
        -n \
        -c /etc/supervisord.conf
fi

###############################################################################
# First Time Configuration
###############################################################################

log "First-time container initialization"

required_vars=(
    ROOT_PASSWD
    NAGIOS_USER
    NAGIOS_USER_PASSWD
    PORT
    Master_IP
    Host_IP
    compute_g_name
    hm_g_name
    gpu_g_name
    master_g_name
    login_g_name
    management_g_name
)

for var in "${required_vars[@]}"; do

    if [[ -z "${!var:-}" ]]; then
        error "Required environment variable '$var' is not set"
    fi

done

export "${required_vars[@]}"
export HOSTNAME="$(hostname)"

###############################################################################
# Initialize Persistent Data
###############################################################################

if [[ -d /nagiosdata.NEEDINIT ]]; then

    log "Initializing nagiosdata"

    rm -rf /nagiosdata 2>/dev/null || true

    cp -a /nagiosdata.NEEDINIT /nagiosdata

    mv /nagiosdata.NEEDINIT \
       /nagiosdata.orig
fi

###############################################################################
# Runtime Directories
###############################################################################

mkdir -p /run/httpd
mkdir -p /var/log/httpd
mkdir -p /var/log/supervisor
mkdir -p /etc/nagios/conf.d

###############################################################################
# Apache Port
###############################################################################

if [[ -n "${PORT}" ]]; then

    log "Configuring Apache port ${PORT}"

    sed -i \
        "s/^Listen 80$/Listen ${PORT}/g" \
        /etc/httpd/conf/httpd.conf

fi

###############################################################################
# Root Password
###############################################################################

log "Updating root password"

echo "root:${ROOT_PASSWD}" | chpasswd

###############################################################################
# Nagios Web Authentication
###############################################################################

log "Creating Nagios web user"

htpasswd -bc \
    /etc/nagios/newpasswd.users \
    "${NAGIOS_USER}" \
    "${NAGIOS_USER_PASSWD}"

###############################################################################
# Nagios Configuration
###############################################################################

if [[ ! -f /etc/nagios/nagios.cfg.orig ]]; then

    cp -a \
        /etc/nagios/nagios.cfg \
        /etc/nagios/nagios.cfg.orig

fi

grep -q "cfg_dir=/etc/nagios/conf.d" \
    /etc/nagios/nagios.cfg || \
    sed -i \
    '/#cfg_dir=\/etc\/nagios\/routers/a cfg_dir=\/etc\/nagios\/conf.d' \
    /etc/nagios/nagios.cfg

###############################################################################
# Apache Nagios Authentication File
###############################################################################

if [[ ! -f /etc/httpd/conf.d/nagios.conf.orig ]]; then

    cp -a \
        /etc/httpd/conf.d/nagios.conf \
        /etc/httpd/conf.d/nagios.conf.orig

fi

sed -i \
's|AuthUserFile /etc/nagios/passwd|AuthUserFile /etc/nagios/newpasswd.users|g' \
/etc/httpd/conf.d/nagios.conf

###############################################################################
# Deploy Templates
###############################################################################

log "Generating Nagios configuration"

mkdir -p /etc/nagios/conf.d

envsubst \
    < /nagios_conf/conf.d/services.cfg.template \
    > /etc/nagios/conf.d/services.cfg


cp -f \
    /nagios_conf/conf.d/commands.cfg \
    /etc/nagios/conf.d/

###############################################################################
# Marker Files
###############################################################################

touch /root/CONFIGURED

echo "${Host_IP}" > /etc/nagios/CONFIGURED

###############################################################################
# Validation
###############################################################################

log "Validating Nagios configuration"

nagios -v /etc/nagios/nagios.cfg || \
    error "Nagios configuration validation failed"

###############################################################################
# Start Services
###############################################################################

log "Starting supervisord..."

exec /usr/bin/supervisord \
    -n \
    -c /etc/supervisord.conf
