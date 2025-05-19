import json
import asyncio
import os
import logging

import aiohttp

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables for FastAPI backend
FASTAPI_BASE_URL = os.environ.get("FASTAPI_BASE_URL")
API_AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN") # For your FastAPI

# Environment variables for the Source Endpoint Service
SOURCE_SERVICE_URL = os.environ.get("SOURCE_SERVICE_URL")
SOURCE_SERVICE_AUTH_TOKEN = os.environ.get("SOURCE_SERVICE_AUTH_TOKEN") # Optional, for the source service

# Basic configuration checks
if not FASTAPI_BASE_URL:
    logger.error("FATAL: FASTAPI_BASE_URL environment variable not set.")
if not API_AUTH_TOKEN:
    logger.error("FATAL: API_AUTH_TOKEN environment variable not set for FastAPI.")
if not SOURCE_SERVICE_URL:
    logger.error("FATAL: SOURCE_SERVICE_URL environment variable not set.")


async def fetch_word_and_meaning_from_source(source_identifier: str, session: aiohttp.ClientSession):
    """
    Fetches word and meaning from the external source service.
    """
    if not SOURCE_SERVICE_URL:
        return {"error": "Source service URL not configured in Lambda", "statusCode": 500, "data": None}

    # Example: Assuming the source service takes an 'id' query parameter
    # Adjust the URL and method (GET/POST) as per the source service's API
    url = f"{SOURCE_SERVICE_URL}/getVocabularyData?id={source_identifier}"
    
    source_headers = {'Content-Type': 'application/json'}
    if SOURCE_SERVICE_AUTH_TOKEN:
        source_headers['Authorization'] = f'Bearer {SOURCE_SERVICE_AUTH_TOKEN}'

    logger.info(f"Calling Source Service GET {url}")
    async with session.get(url, headers=source_headers) as response:
        response_text = await response.text()
        logger.info(f"Source Service response status: {response.status}, body: {response_text}")
        
        if response.status == 200:
            try:
                data = json.loads(response_text)
                word = data.get('word')
                meaning = data.get('meaning')
                if not word or not meaning:
                    logger.error("Source service response missing 'word' or 'meaning'.")
                    return {"statusCode": 500, "error": "Invalid data from source service", "data": None}
                return {"statusCode": 200, "word": word, "meaning": meaning, "data": data}
            except json.JSONDecodeError:
                logger.error(f"Failed to parse JSON from source service: {response_text}")
                return {"statusCode": 500, "error": "Invalid JSON from source service", "data": None}
        else:
            return {
                "statusCode": response.status,
                "error": "Failed to fetch data from source service",
                "details": response_text,
                "data": None
            }


async def create_vocabulary_in_fastapi(word: str, meaning: str, session: aiohttp.ClientSession):
    """
    Calls your FastAPI backend to create the vocabulary entry.
    """
    if not FASTAPI_BASE_URL or not API_AUTH_TOKEN:
        return {"error": "FastAPI backend not configured in Lambda (URL or Token)", "statusCode": 500}

    url = f"{FASTAPI_BASE_URL}/vocabularies/"
    payload = {'word': word, 'meaning': meaning}
    headers = {
        'Authorization': f'Bearer {API_AUTH_TOKEN}',
        'Content-Type': 'application/json'
    }

    logger.info(f"Calling FastAPI POST {url} with payload: {payload}")
    async with session.post(url, json=payload, headers=headers) as response:
        response_text = await response.text()
        logger.info(f"FastAPI response status: {response.status}, body: {response_text}")
        try:
            response_json = json.loads(response_text)
        except json.JSONDecodeError:
            response_json = {"raw_response": response_text}
        
        return {
            "statusCode": response.status,
            "body": response_json
        }


async def main(event_body):
    try:
        # The Lambda now expects an identifier for the source service,
        # e.g., an ID to fetch the specific word/meaning.
        source_identifier = event_body.get('source_identifier') # Or any other key you define

        if not source_identifier:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing 'source_identifier' in request body"})
            }

        async with aiohttp.ClientSession() as session:
            # 1. Fetch data from the source service
            source_data_response = await fetch_word_and_meaning_from_source(source_identifier, session)

            if source_data_response["statusCode"] != 200:
                logger.error(f"Failed to get data from source service: {source_data_response.get('error')}")
                # Return the error from the source service attempt
                return {
                    "statusCode": source_data_response["statusCode"],
                    "body": json.dumps({
                        "error": "Failed to retrieve vocabulary data from source",
                        "source_service_details": source_data_response.get("details") or source_data_response.get("error")
                    })
                }
            
            word_to_create = source_data_response['word']
            meaning_to_create = source_data_response['meaning']

            # 2. Create vocabulary in your FastAPI backend using fetched data
            fastapi_result = await create_vocabulary_in_fastapi(
                word=word_to_create,
                meaning=meaning_to_create,
                session=session
            )
            return fastapi_result

    except Exception as e:
        logger.error(f"Error in main_async: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error in Lambda", "details": str(e)})
        }

def lambda_handler(event, context):
    logger.info(f"Received event: {event}")

    try:
        if isinstance(event.get('body'), str):
            event_body = json.loads(event.get('body', '{}'))
        else:
            event_body = event.get('body', {})
            
    except json.JSONDecodeError:
        logger.error("Invalid JSON in request body.")
        return {
            'statusCode': 400,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'error': 'Invalid JSON in request body'})
        }

    loop = asyncio.get_event_loop()
    if loop.is_running():
        task = asyncio.ensure_future(main(event_body))
        result = loop.run_until_complete(task)
    else:
        result = asyncio.run(main(event_body))
    
    if isinstance(result.get("body"), dict) or isinstance(result.get("body"), list):
        result["body"] = json.dumps(result["body"])
        
    return result