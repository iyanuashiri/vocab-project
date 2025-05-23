# from sqlmodel import create_engine, Session, SQLModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base

from decouple import config


sqlite_file_name = config("CHAPERONE_SQLITE_FILE_NAME")
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, echo=True)


def create_db_and_tables():
    Base.metadata.create_all(engine)
    
    
def get_session():
    with Session(engine) as session:
        yield session
