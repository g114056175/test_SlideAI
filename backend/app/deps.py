from dotenv import load_dotenv
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError, ExpiredSignatureError
from datetime import datetime, timedelta, date
from backend.app.models import User, UsageRecord, UserFile, Project, Base
from typing import Generator
from fastapi import HTTPException, Depends

# Load env
project_root = os.path.dirname(os.path.dirname(__file__))
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path=dotenv_path)

# Keep local-only deployments self-contained: persistent application data lives
# under the project data directory rather than beside source files.
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./data/database/slideai.sqlite3')
SECRET_KEY = os.getenv('SECRET_KEY', 'devsecret')
ALGORITHM = os.getenv('ALGORITHM', 'HS256')
DAILY_USAGE_LIMIT = int(os.getenv('DAILY_USAGE_LIMIT', '5'))
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', str(50 * 1024 * 1024)))
FILE_RETENTION_DAYS = int(os.getenv('FILE_RETENTION_DAYS', '7'))
FILE_EXPIRY_WARNING_HOURS = int(os.getenv('FILE_EXPIRY_WARNING_HOURS', '24'))

# Handle postgres URL format
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Ensure models created
Base.metadata.create_all(bind=engine)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

# Dependency
def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict) -> str:
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return token

def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail='Token 已過期')
    except JWTError:
        raise HTTPException(status_code=401, detail='Token 無效')

# current_user dependency
from fastapi import Depends
from sqlalchemy.orm import Session

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    payload = decode_access_token(token)
    email = payload.get('sub')
    if not email:
        raise HTTPException(status_code=401, detail='Token 格式錯誤')
    user = db.query(User).filter_by(email=email).first()
    if not user:
        raise HTTPException(status_code=401, detail='使用者不存在')
    return user

# Usage helpers

def check_daily_usage_limit(user: User, db: Session) -> bool:
    if user.is_admin:
        return True
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    today_usage = db.query(UsageRecord).filter(
        UsageRecord.user_id == user.id,
        UsageRecord.usage_date >= today_start,
        UsageRecord.usage_date <= today_end
    ).count()
    return today_usage < DAILY_USAGE_LIMIT


def record_usage(user: User, service_type: str, db: Session):
    if user.is_admin:
        return
    usage_record = UsageRecord(
        user_id=user.id,
        service_type=service_type,
        usage_date=datetime.utcnow()
    )
    db.add(usage_record)
    db.commit()


def create_file_record(user: User, file_name: str, file_path: str, file_type: str, file_size: int, db: Session):
    expires_at = datetime.utcnow() + timedelta(days=FILE_RETENTION_DAYS)
    file_record = UserFile(
        user_id=user.id,
        file_name=file_name,
        file_path=file_path,
        file_type=file_type,
        file_size=file_size,
        expires_at=expires_at
    )
    db.add(file_record)
    db.commit()
    return file_record


def cleanup_expired_files(db: Session):
    """清理過期檔案"""
    now = datetime.utcnow()
    expired_files = db.query(UserFile).filter(
        UserFile.expires_at < now,
        UserFile.status != 'expired'
    ).all()

    for file_record in expired_files:
        try:
            if os.path.exists(file_record.file_path):
                os.remove(file_record.file_path)
            file_record.status = 'expired'
            db.commit()
        except Exception as e:
            # swallow errors here; callers may log
            pass


def get_expiring_files(db: Session, hours: int = FILE_EXPIRY_WARNING_HOURS):
    now = datetime.utcnow()
    warning_time = now + timedelta(hours=hours)
    return db.query(UserFile).filter(
        UserFile.expires_at <= warning_time,
        UserFile.expires_at > now,
        UserFile.status == 'completed'
    ).all()


# ---------------------------------------------------------------------------
# Project helpers
# ---------------------------------------------------------------------------

def create_project_record(
    user: User,
    project_name: str,
    pdf_path: str,
    script_json: str,
    db: Session,
    pdf_id: str = None,
) -> Project:
    """
    Insert a new project row with status='processing'.
    Returns the newly created Project ORM object (with its id populated).
    """
    project = Project(
        user_id=user.id,
        project_name=project_name,
        pdf_id=pdf_id,
        pdf_path=pdf_path,
        script_json=script_json,
        status='processing',
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def update_project_video_url(
    project_id: int,
    video_url: str,
    script_json: str,
    db: Session,
) -> None:
    """
    Set video_url and script_json on an existing project, then mark it
    as 'completed'.  No-op if the project_id does not exist.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if project is None:
        return
    project.video_url = video_url
    if script_json is not None:
        project.script_json = script_json
    project.status = 'completed'
    db.commit()


def delete_project(
    project_id: int,
    user_id: int,
    db: Session,
) -> dict:
    """
    Soft-delete a project belonging to the given user.

    Steps:
      1. Verify the project exists and belongs to user_id.
      2. Delete the video file from disk if it exists.
      3. Set status='deleted' (soft delete -- row is kept for audit).

    Returns a dict with keys:
      - 'found'     (bool) whether a matching non-deleted project was found
      - 'forbidden' (bool) whether the project belongs to a different user
      - 'file_deleted' (bool) whether the disk file was removed
    """
    project = db.query(Project).filter(Project.id == project_id).first()

    if project is None or project.status == 'deleted':
        return {'found': False, 'forbidden': False, 'file_deleted': False}

    if project.user_id != user_id:
        return {'found': True, 'forbidden': True, 'file_deleted': False}

    file_deleted = False
    if project.video_url and os.path.exists(project.video_url):
        try:
            os.remove(project.video_url)
            file_deleted = True
        except OSError:
            pass

    # Remove persistent thumbnail folder if it exists
    if project.pdf_id:
        thumb_dir = os.path.join(
            os.path.dirname(__file__), "user_thumbnails", project.pdf_id
        )
        thumb_dir = os.path.abspath(thumb_dir)
        if os.path.isdir(thumb_dir):
            import shutil as _shutil
            try:
                _shutil.rmtree(thumb_dir)
            except OSError:
                pass

    project.status = 'deleted'
    db.commit()
    return {'found': True, 'forbidden': False, 'file_deleted': file_deleted}
