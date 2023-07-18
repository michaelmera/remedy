#!/usr/bin/env bash

BIN_DIR=$(cd $(dirname "${BASH_SOURCE[0]}") && pwd)

pyrcc5 -o "${BIN_DIR}/../remedy/gui/resources.py" "${BIN_DIR}/../resources.qrc"
