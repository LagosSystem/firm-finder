#!/usr/bin/env bash
# Exit immediately if a command exits with a non-zero status
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium --with-deps

