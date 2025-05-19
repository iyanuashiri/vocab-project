from typing import Annotated, Optional
from datetime import timedelta
import json

from fastapi import FastAPI, Depends, status, HTTPException
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from momento import CacheClient, Configurations, CredentialProvider
from momento.responses import CacheGet, CreateCache

from . import models
from . import schemas
from app.core.database import create_db_and_tables, get_session
from app.core.security import generate_hashed_password, verify_hashed_password, manager, OAuth2PasswordNewRequestForm
from app.prompts import generate_associations
from app.core.config import settings


SessionDep = Annotated[Session, Depends(get_session)]

MOMENTO_API_KEY = settings.MOMENTO_API_KEY

# Momento Cache setup
def create_momento_client():
    momento_api_key = CredentialProvider.from_string(MOMENTO_API_KEY)
    ttl = timedelta(seconds=int(settings.MOMENTO_TTL_SECONDS))
    config = {
        'configuration': Configurations.Laptop.v1(),
        'credential_provider': momento_api_key,
        'default_ttl': ttl
    }
    return CacheClient.create(**config)


# Create a cache dependency MomentoClientDep
def get_momento_client():
    client = create_momento_client()
    try:
        yield client
    finally:
        client.close()

MomentoClientDep = Annotated[CacheClient, Depends(get_momento_client)]

# Cache name constant
ASSOCIATIONS_CACHE_NAME = "user_associations"

app = FastAPI()

origins = [
    "http://localhost:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


@manager.user_loader()
async def get_user(email: str = None):
    session = next(get_session())
    return session.query(models.User).filter(models.User.email == email).first()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    
    # Create Momento cache at startup
    client = create_momento_client()
    resp = client.create_cache(ASSOCIATIONS_CACHE_NAME)
    match resp:
        case CreateCache.Success():
            print(f"Momento cache '{ASSOCIATIONS_CACHE_NAME}' created or already exists.")
        case CreateCache.Error() as error:
            print(f"Error creating Momento cache: {error.message}")
        case _:
            print("Unreachable error state")
    # client.close()


@app.post("/login/", status_code=status.HTTP_200_OK)
async def login(session: SessionDep, data: OAuth2PasswordNewRequestForm = Depends()):
    email = data.email
    password = data.password

    user = session.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email not correct",
                            headers={"WWW-Authenticate": "Bearer"})

    if not verify_hashed_password(raw_password=password, hashed_password=user.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Password not correct",
                            headers={"WWW-Authenticate": "Bearer"})

    access_token = manager.create_access_token(data={"sub": email}, expires=timedelta(hours=12))
    return {"access_token": access_token, "token_type": "bearer", "email": email}
    
    
@app.post("/users/", status_code=status.HTTP_201_CREATED, response_model=schemas.UserRead)
async def create_user(user: schemas.UserCreate, session: SessionDep) -> schemas.UserRead:
    existing_user = session.query(models.User).filter(models.User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    db_user = models.User(first_name=user.first_name, last_name=user.last_name, email=user.email, password=user.password)
    db_user.set_password(user.password)
    session.add(db_user)
    session.commit()
    session.refresh(db_user)
    return db_user  


@app.get("/users/", response_model=list[schemas.UserRead])
async def get_users(session: SessionDep, ) -> list[schemas.UserRead]:
    users = session.query(models.User).all()
    return users
    
    
@app.get("/users/{user_id}/", response_model=schemas.UserRead)
async def get_users(user_id: int, session: SessionDep) -> schemas.UserRead:
    user = session.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.post("/vocabularies/", status_code=status.HTTP_201_CREATED, response_model=schemas.VocabularyRead)
async def create_vocabulary(vocab: schemas.VocabularyCreate, session: SessionDep, current_user: models.User = Depends(manager)) -> schemas.VocabularyRead:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")

    db_vocab = models.Vocabulary(word=vocab.word, meaning=vocab.meaning)
    session.add(db_vocab)
    session.commit()
    session.refresh(db_vocab)
    return db_vocab


@app.get("/vocabularies/", response_model=list[schemas.VocabularyRead])
async def get_vocabularies(session: SessionDep, current_user: models.User = Depends(manager)) -> list[schemas.VocabularyRead]:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")
    vocabularies = session.query(models.Vocabulary).all()
    return vocabularies


@app.get("/vocabularies/{vocab_id}/", response_model=schemas.VocabularyRead)
async def get_vocabulary_by_id(vocab_id: int, session: SessionDep, current_user: models.User = Depends(manager)) -> schemas.VocabularyRead:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")
        
    vocab = session.get(models.Vocabulary, vocab_id)
    if not vocab:
        raise HTTPException(status_code=404, detail="Vocabulary not found")
    return vocab


@app.post("/associations/", status_code=status.HTTP_201_CREATED, response_model=schemas.AssociationRead)
async def create_association(
    association: schemas.AssociationCreate, 
    session: SessionDep, 
    momento_client: MomentoClientDep,
    current_user: models.User = Depends(manager)
) -> schemas.AssociationRead:
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")

    vocab = session.get(models.Vocabulary, association.vocabulary_id)
    if not vocab:
        raise HTTPException(status_code=404, detail="Vocabulary not found")

    generated_associations = await generate_associations(vocabulary=vocab.word, number_of_options=3)
    generated_associations = generated_associations[0]
    print(generated_associations)

    db_association = models.Association(user_id=current_user.id, vocabulary_id=vocab.id)
    session.add(db_association)
    session.commit()
    session.refresh(db_association)
    
    for option, meaning in generated_associations['options'].items():
        if option.isupper():
            is_correct = True
        else:
            is_correct = False
        db_option = models.Option(option=option, meaning=meaning, is_correct=is_correct, association_id=db_association.id)
        session.add(db_option)
        session.commit()
        session.refresh(db_option)
    
    # Invalidate cache after adding new association
    cache_key = f"user_associations_{current_user.id}"
    momento_client.delete(ASSOCIATIONS_CACHE_NAME, cache_key)

    return db_association


@app.get("/associations/", response_model=list[schemas.AssociationRead])
async def get_associations(
    session: SessionDep, 
    momento_client: MomentoClientDep,
    current_user: models.User = Depends(manager)
) -> list[schemas.AssociationRead]:
    """Get all associations for the current user, using Momento Cache for performance"""
    
    # Create a cache key based on the user ID
    cache_key = f"user_associations_{current_user.id}"
    
    # Try to get from cache first
    cache_resp = momento_client.get(ASSOCIATIONS_CACHE_NAME, cache_key)
    
    match cache_resp:
        case CacheGet.Hit():
            # Cache hit - return the cached data
            print("Cache hit for user associations")
            cached_data = json.loads(cache_resp.value_string)
            return [schemas.AssociationRead.parse_obj(assoc) for assoc in cached_data]
        
        case CacheGet.Miss():
            # Cache miss - query the database
            print("Cache miss for user associations - querying database")
            associations = session.query(models.Association).order_by(
                models.Association.id.desc()
            ).filter(
                models.Association.user_id == current_user.id, 
                models.Association.status == "pending"
            ).all()
            
            # Store in cache for future requests
            # Convert associations to a JSON-serializable list
            associations_data = [assoc.__dict__ for assoc in associations]
            
            # Remove SQLAlchemy state attributes
            for assoc in associations_data:
                if '_sa_instance_state' in assoc:
                    del assoc['_sa_instance_state']
            
            # Store in cache with default TTL
            momento_client.set(ASSOCIATIONS_CACHE_NAME, cache_key, json.dumps(associations_data))
            
            return associations
        
        case CacheGet.Error() as error:
            # Handle cache error gracefully by falling back to database
            print(f"Momento cache error: {error.message}. Falling back to database.")
            associations = session.query(models.Association).order_by(
                models.Association.id.desc()
            ).filter(
                models.Association.user_id == current_user.id, 
                models.Association.status == "pending"
            ).all()
            return associations
        
        case _:
            # Unreachable but handle gracefully
            print("Unreachable cache state. Falling back to database.")
            associations = session.query(models.Association).order_by(
                models.Association.id.desc()
            ).filter(
                models.Association.user_id == current_user.id, 
                models.Association.status == "pending"
            ).all()
            return associations


@app.get("/associations/{association_id}", response_model=schemas.AssociationRead)
async def get_association(
    association_id: int, 
    session: SessionDep, 
    momento_client: MomentoClientDep,
    current_user: models.User = Depends(manager)
) -> schemas.AssociationRead:
    """Get a specific association by ID"""
    # Create a cache key for this specific association
    cache_key = f"association_{current_user.id}_{association_id}"
    
    # Try to get from cache first
    cache_resp = momento_client.get(ASSOCIATIONS_CACHE_NAME, cache_key)
    
    match cache_resp:
        case CacheGet.Hit():
            # Cache hit
            print(f"Cache hit for association {association_id}")
            cached_data = json.loads(cache_resp.value_string)
            return schemas.AssociationRead.parse_obj(cached_data)
            
        case CacheGet.Miss():
            # Cache miss - query the database
            print(f"Cache miss for association {association_id} - querying database")
            association = session.query(models.Association).filter(
                models.Association.id == association_id,
                models.Association.user_id == current_user.id
            ).first()
            
            if not association:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Association not found"
                )
            
            # Store in cache for future requests
            association_data = association.__dict__.copy()
            if '_sa_instance_state' in association_data:
                del association_data['_sa_instance_state']
                
            momento_client.set(ASSOCIATIONS_CACHE_NAME, cache_key, json.dumps(association_data))
            
            return association
            
        case CacheGet.Error() as error:
            # Handle cache error by falling back to database
            print(f"Cache error: {error.message}. Falling back to database.")
            association = session.query(models.Association).filter(
                models.Association.id == association_id,
                models.Association.user_id == current_user.id
            ).first()
            
            if not association:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Association not found"
                )
            
            return association
            
        case _:
            # Unreachable but handle gracefully
            association = session.query(models.Association).filter(
                models.Association.id == association_id,
                models.Association.user_id == current_user.id
            ).first()
            
            if not association:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Association not found"
                )
            
            return association


@app.put("/associations/{association_id}/correct", response_model=schemas.AssociationRead)
async def update_association_correct(
    association_id: int, 
    session: SessionDep, 
    momento_client: MomentoClientDep,
    current_user: models.User = Depends(manager)
) -> schemas.AssociationRead:
    """Update a specific association by ID"""
    association = session.query(models.Association).filter(
        models.Association.id == association_id,
        models.Association.user_id == current_user.id
    ).first()
    
    if not association:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Association not found"
        )
    
    association.correct_option()
    session.add(association)
    session.commit()
    session.refresh(association)
    
    # Invalidate caches after update
    user_key = f"user_associations_{current_user.id}"
    assoc_key = f"association_{current_user.id}_{association_id}"
    
    momento_client.delete(ASSOCIATIONS_CACHE_NAME, user_key)
    momento_client.delete(ASSOCIATIONS_CACHE_NAME, assoc_key)
    
    return association


@app.put("/associations/{association_id}/incorrect", response_model=schemas.AssociationRead)
async def update_association_incorrect(
    association_id: int, 
    session: SessionDep, 
    momento_client: MomentoClientDep,
    current_user: models.User = Depends(manager)
) -> schemas.AssociationRead:
    """Update a specific association by ID"""
    association = session.query(models.Association).filter(
        models.Association.id == association_id,
        models.Association.user_id == current_user.id
    ).first()
    
    if not association:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Association not found"
        )
    
    association.incorrect_option()
    session.add(association)
    session.commit()
    session.refresh(association)
    
    # Invalidate caches after update
    user_key = f"user_associations_{current_user.id}"
    assoc_key = f"association_{current_user.id}_{association_id}"
    
    momento_client.delete(ASSOCIATIONS_CACHE_NAME, user_key)
    momento_client.delete(ASSOCIATIONS_CACHE_NAME, assoc_key)
    
    return association