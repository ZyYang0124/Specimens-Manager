# -*- coding: utf-8 -*-
"""基本配置：端口、数据库位置、系统名称、访问策略

所有路径/端口均可被 SPM_ 前缀的环境变量覆盖（测试/开发隔离用，
生产环境不设这些变量时行为与写死的默认值完全一致）：
  SPM_PORT          服务监听端口（默认 8000）
  SPM_DB_FILE       数据库文件名（相对本目录，默认 data.db）
  SPM_UPLOADS_DIR   附件目录名（相对本目录，默认 uploads）
"""
import os

# 系统名称（显示在登录页、顶栏和浏览器标题）
SYSTEM_NAME = "标本信息管理系统"

# 服务监听端口（局域网协作者访问 http://<你的IP>:8000）
PORT = int(os.environ.get("SPM_PORT", "8000"))
HOST = "0.0.0.0"

# 数据库文件（相对本文件所在目录；备份整个文件即可完整备份）
DB_FILE = os.environ.get("SPM_DB_FILE", "data.db")

# 附件目录（相对本文件所在目录；空 = 默认 uploads）
UPLOADS_DIR = os.environ.get("SPM_UPLOADS_DIR", "uploads")

# 是否开放自助注册：True = 协作者可自行注册账号；False = 仅管理员在用户管理里添加
ALLOW_REGISTER = True

# ---- 备份留存 / 容量 / 异地副本 / RPO·RTO 目标（可用环境变量覆盖） ----
BACKUP_DIR = os.environ.get("SPM_BACKUP_DIR", "backups")            # 备份目录（相对本目录）
BACKUP_KEEP = int(os.environ.get("SPM_BACKUP_KEEP", "30"))          # 留存包数量
BACKUP_MAX_GB = float(os.environ.get("SPM_BACKUP_MAX_GB", "20"))    # 备份总容量上限（GB）
BACKUP_OFFSITE_DIR = os.environ.get("SPM_BACKUP_OFFSITE_DIR", "")   # 异地副本目录；空 = 未配置
RPO_TARGET_MIN = int(os.environ.get("SPM_RPO_TARGET_MIN", "1440"))  # 恢复点目标（分钟）
RTO_TARGET_SEC = int(os.environ.get("SPM_RTO_TARGET_SEC", "1800"))  # 恢复时间目标（秒）
