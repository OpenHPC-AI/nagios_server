#!/bin/bash

set -Eeuo pipefail

###############################################################################
# Files
###############################################################################

NAGIOS_CONF_DIR="/etc/nagios/conf.d"

HOSTS_CFG="${NAGIOS_CONF_DIR}/hosts.cfg"
SERVICES_CFG="${NAGIOS_CONF_DIR}/services.cfg"

###############################################################################
# Globals
###############################################################################

declare -a SELECTED_GROUPS
declare -A NODE_TYPE_MAP

NODE_TYPE_MAP=(
    [master]="master"
    [mgmt]="management"
    [login]="login"
    [compute]="compute"
    [hm]="high memory"
    [gpu]="gpu"
)

###############################################################################
# Utility
###############################################################################

log() {
    echo "[INFO] $*"
}

error() {
    echo "[ERROR] $*" >&2
    exit 1
}

###############################################################################
# Backup
###############################################################################

backup_configs() {

    mkdir -p "${NAGIOS_CONF_DIR}"

    for FILE in \
        "${HOSTS_CFG}" \
        "${SERVICES_CFG}"
    do
        if [[ -f "${FILE}" ]]; then
            cp -f "${FILE}" "${FILE}.bak"
        fi
    done
}

###############################################################################
# Fresh Files
###############################################################################

create_empty_files() {

    : > "${HOSTS_CFG}"
    : > "${SERVICES_CFG}"
}

###############################################################################
# User Selection
###############################################################################

ask_group() {

    local DISPLAY_NAME="$1"
    local GROUP_NAME="$2"

    local ANSWER

    read -rp "Add ${DISPLAY_NAME} nodes? (y/n): " ANSWER

    case "${ANSWER,,}" in
        y|yes)
            SELECTED_GROUPS+=("${GROUP_NAME}")
            ;;
    esac
}

select_groups() {

    ask_group "MASTER" "master"
    ask_group "MANAGEMENT" "mgmt"
    ask_group "LOGIN" "login"
    ask_group "COMPUTE" "compute"
    ask_group "HIGH MEMORY" "hm"
    ask_group "GPU" "gpu"

    [[ ${#SELECTED_GROUPS[@]} -gt 0 ]] || \
        error "No hostgroups selected"
}

###############################################################################
# Template
###############################################################################

create_host_template() {

    local TEMPLATE_NAME="$1"

    cat >> "${HOSTS_CFG}" <<EOF

define host{
        name                    ${TEMPLATE_NAME}
        use                     generic-host
        check_period            24x7
        check_interval          5
        retry_interval          1
        max_check_attempts      10
        check_command           check-host-alive
        notification_period     24x7
        notification_interval   30
        notification_options    d,r
        contact_groups          admins
        register                0
}

EOF
}


###############################################################################
# Hosts
###############################################################################

generate_hosts() {

    local TEMPLATE_NAME="$1"

    for GROUP in "${SELECTED_GROUPS[@]}"
    do

        local NODE_TYPE="${NODE_TYPE_MAP[$GROUP]}"

        case "${GROUP}" in

            master|mgmt|login)

                log "Generating ${GROUP} nodes"

                python3 add_service_node_def.py \
                    "${HOSTS_CFG}" \
                    "${TEMPLATE_NAME}" \
                    --node_type "${NODE_TYPE}"
                ;;

            compute|hm|gpu)

                log "Generating ${GROUP} nodes"

                python3 add_compute_node_def.py \
                    "${HOSTS_CFG}" \
                    "${TEMPLATE_NAME}" \
                    --node_type "${NODE_TYPE}"
                ;;

        esac

    done
}

###############################################################################
# Fix Template References
###############################################################################

fix_template_references() {

    local TEMPLATE_NAME="$1"

    sed -i \
        -e "s/use[[:space:]]\+PARAM ARYABHATTA/use ${TEMPLATE_NAME}/g" \
        -e "s/use[[:space:]]\+nagios.cfg.template/use ${TEMPLATE_NAME}/g" \
        "${HOSTS_CFG}"
}

###############################################################################
# Services
###############################################################################

generate_services() {

    local HOSTGROUP_LIST

    HOSTGROUP_LIST=$(IFS=, ; echo "${SELECTED_GROUPS[*]}")

    cat > "${SERVICES_CFG}" <<EOF

define service{
        use                     generic-service
        hostgroup_name          ${HOSTGROUP_LIST}
        service_description     Current Users
        check_command           check_nrpe!check_users
}

define service{
        use                     generic-service
        hostgroup_name          ${HOSTGROUP_LIST}
        service_description     Zombie Processes
        check_command           check_nrpe!check_zombie_procs
}

define service{
        use                     generic-service
        hostgroup_name          ${HOSTGROUP_LIST}
        service_description     Node Load
        check_command           check_nrpe!check_load
}

define service{
        use                     generic-service
        hostgroup_name          ${HOSTGROUP_LIST}
        service_description     Check IB
        check_command           check_nrpe!check_ib
}

define service{
        use                     generic-service
        hostgroup_name          ${HOSTGROUP_LIST}
        service_description     Check NTP
        check_command           check_nrpe!check_ntp
}

EOF

    #
    # Only if master or mgmt selected
    #
    if [[ " ${SELECTED_GROUPS[*]} " =~ " master " ]] || \
       [[ " ${SELECTED_GROUPS[*]} " =~ " mgmt " ]]
    then

        cat >> "${SERVICES_CFG}" <<EOF

define service{
        use                     generic-service
        hostgroup_name          master,mgmt
        service_description     Httpd Status
        check_command           check_nrpe!check_httpd
}

EOF
    fi

    #
    # Only if Slurm nodes selected
    #
    local SLURM_GROUPS=()

    for G in compute hm gpu master
    do
        [[ " ${SELECTED_GROUPS[*]} " =~ " ${G} " ]] && \
            SLURM_GROUPS+=("${G}")
    done

    if [[ ${#SLURM_GROUPS[@]} -gt 0 ]]
    then

        local SLURM_LIST

        SLURM_LIST=$(IFS=, ; echo "${SLURM_GROUPS[*]}")

        cat >> "${SERVICES_CFG}" <<EOF

define service{
        use                     generic-service
        hostgroup_name          ${SLURM_LIST}
        service_description     Slurm Status
        check_command           check_nrpe!check_slurmd
}

EOF

    fi
}

###############################################################################
# Validation
###############################################################################

validate_config() {

    log "Validating Nagios configuration"

    nagios -v /etc/nagios/nagios.cfg
}

###############################################################################
# Main
###############################################################################

main() {

    read -rp "Enter Template Name: " TEMPLATE_NAME

    [[ -n "${TEMPLATE_NAME}" ]] || \
        error "Template name cannot be empty"

    backup_configs

    create_empty_files

    select_groups

    create_host_template "${TEMPLATE_NAME}"


    generate_hosts "${TEMPLATE_NAME}"

    fix_template_references "${TEMPLATE_NAME}"

    generate_services

    validate_config

    log "Configuration generated successfully"

    echo
    echo "Selected Groups : ${SELECTED_GROUPS[*]}"
    echo "Template Name   : ${TEMPLATE_NAME}"
    echo
}

main "$@"
