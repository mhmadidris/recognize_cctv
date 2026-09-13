from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.engine import URL
from app.utils.env import env

# Special handling for postgresql vs postgres (SQLAlchemy 1.4+ requirement)
db_driver = "postgresql" if env.DB_TYPE == "postgres" else env.DB_TYPE

# Construct database URL safely as an object (masks password when printed)
db_url_obj = URL.create(
    drivername=db_driver,
    username=env.DB_USERNAME,
    password=env.DB_PASSWORD if env.DB_PASSWORD else None,
    host=env.DB_HOST,
    port=env.DB_PORT,
    database=env.DB_NAME
)

# Pass the URL object directly to create_engine. 
# SQLAlchemy will extract the password internally without exposing it in tracebacks.
engine = create_engine(db_url_obj, echo=env.IS_DEBUG and not env.IS_PRODUCTION)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
