#!/usr/bin/env python3
"""Compatibility entry point: read-only configuration verification; no provisioning."""
import argparse
from deploy_config import paths, preflight, ConfigError

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env'); parser.add_argument('--home')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    try:
        root,source=paths(args.home,args.env)
        preflight(root,source)
        print('[OK] 配置一致；未写入配置，未改动数据库')
        return 0
    except ConfigError as error:
        print('[失败] '+str(error)); return 1

if __name__=='__main__':raise SystemExit(main())
