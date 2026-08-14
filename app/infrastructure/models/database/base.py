"""
数据库 ORM 基类
所有模型共用同一个 declarative_base 实例
"""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
