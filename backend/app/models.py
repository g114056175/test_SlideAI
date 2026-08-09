from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Index, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime, date

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)  # 統一名稱
    is_admin = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    reset_token = Column(String, nullable=True, index=True)  # 新增重設密碼token欄位

    # 關聯到使用記錄、檔案和專案
    usage_records = relationship("UsageRecord", back_populates="user")
    files = relationship("UserFile", back_populates="user")
    projects = relationship("Project", back_populates="user")

class UsageRecord(Base):
    __tablename__ = 'usage_records'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    usage_date = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    service_type = Column(String, nullable=False, index=True)  # 'video_abstract' 或 'ppt_to_video'
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # 關聯到使用者
    user = relationship("User", back_populates="usage_records")

class UserFile(Base):
    __tablename__ = 'user_files'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    file_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)  # 檔案在伺服器上的路徑
    file_type = Column(String, nullable=False, index=True)  # 'video_abstract' 或 'ppt_to_video'
    file_size = Column(Integer, nullable=False)  # 檔案大小 (bytes)
    status = Column(String, default='processing', index=True)  # 'processing', 'completed', 'expired'
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)  # 檔案過期時間
    analysis_result = Column(Text, nullable=True)  # 分析結果 (文字摘要)

    # 關聯到使用者
    user = relationship("User", back_populates="files")

class Project(Base):
    __tablename__ = 'projects'
    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    project_name = Column(String, nullable=False)          # 預設為 PDF 檔名
    pdf_id       = Column(String, nullable=True, index=True)  # UUID assigned at upload time
    pdf_path     = Column(String, nullable=True)           # 原始上傳路徑（暫存用）
    video_url    = Column(String, nullable=True)           # 影片產出後填入
    script_json  = Column(Text, nullable=True)             # Gemini 生成的腳本 (JSON 字串)
    status       = Column(String, default='processing', index=True)  # 'processing'|'completed'|'deleted'
    created_at   = Column(DateTime, default=datetime.utcnow, index=True)

    # 關聯到使用者
    user = relationship("User", back_populates="projects")


# 添加複合索引以優化常用查詢
Index('idx_usage_user_date', UsageRecord.user_id, UsageRecord.usage_date)
Index('idx_files_user_status', UserFile.user_id, UserFile.status)
Index('idx_files_expires', UserFile.expires_at, UserFile.status)
Index('idx_projects_user_status', Project.user_id, Project.status)