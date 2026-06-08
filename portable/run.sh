#!/bin/bash

if ! docker images | awk '{print $1}' | grep  nagios; then
  docker load < cdac_nagios.img
else
  echo "Image 'nagios' already exists."
fi

export $(cat ./.env) > /dev/null 2>&1;docker-compose up -d

echo "wait for a minute to start container......"
