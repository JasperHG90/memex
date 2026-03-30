#!/usr/bin/env python3
"""Resolve Memex config and output shell-sourceable variables.

Outputs:
  MEMEX_RESOLVED_URL='<server_url>'
  MEMEX_RESOLVED_API_KEY='<api_key_or_empty>'

Uses the full config resolution chain:
  env vars -> local .memex.yaml -> global ~/.config/memex/config.yaml -> defaults
"""

import shlex

from memex_common.config import parse_memex_config

config = parse_memex_config()
print(f'MEMEX_RESOLVED_URL={shlex.quote(config.server_url)}')
if config.api_key:
    print(f'MEMEX_RESOLVED_API_KEY={shlex.quote(config.api_key.get_secret_value())}')
else:
    print("MEMEX_RESOLVED_API_KEY=''")
