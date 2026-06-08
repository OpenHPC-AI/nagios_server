FROM rockylinux:9.6

LABEL maintainer="CDAC HPC Team"

ENV container=docker

RUN dnf -y update && \
    dnf -y install epel-release && \
    dnf -y install \
        httpd \
        perl \
        nagios \
        nrpe \
        supervisor \
        gettext \
        python3 \
        python3-pip \
        procps-ng \
        net-tools \
        hostname \
        sudo \
        which \
        iproute \
        wget \
        tar \
        unzip \
        initscripts

RUN     dnf -y install \
        nagios-plugins \
        nagios-plugins-load \
        nagios-plugins-users \
        nagios-plugins-procs \
        nagios-plugins-ping \
        nagios-plugins-http \
        nagios-plugins-swap \
        nagios-plugins-disk \
        nagios-plugins-ssh \
        nagios-plugins-nrpe && \
    dnf clean all
#
# Nagios configuration templates
#
COPY etc_nagios/ /nagios_conf/

#
# HTTPD init script
#
COPY httpd_initscripts /etc/init.d/httpd

#
# Supervisor configuration
#
COPY supervisord.conf /etc/supervisord.conf

#
# Entrypoint
#
COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && \
    chmod +x /etc/init.d/httpd && \
    mkdir -p /run/httpd && \
    mkdir -p /run/php-fpm && \
    mkdir -p /etc/nagios/conf.d && \
    mkdir -p /var/log/supervisor

EXPOSE 80
EXPOSE 5666
EXPOSE 5667
EXPOSE 5668
EXPOSE 9001

ENTRYPOINT ["/entrypoint.sh"]
