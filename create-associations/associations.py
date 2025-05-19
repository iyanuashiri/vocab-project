import json
import asyncio
import os
import logging

import aiohttp
# from decouple import config # Using os.environ directly for Lambda simplicity

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
FASTAPI_BASE_URL = os.environ.get("FASTAPI_BASE_URL")
API_AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN") 

if not FASTAPI_BASE_URL:
    logger.error("FATAL: FASTAPI_BASE_URL environment variable not set.")
if not API_AUTH_TOKEN:
    logger.error("FATAL: API_AUTH_TOKEN environment variable not set.")


async def get_vocabulary_from_fastapi(vocabulary_id: int, session: aiohttp.ClientSession):
    """
    Optional: Fetches vocabulary details.
    The FastAPI POST /associations/ endpoint already validates the vocabulary_id,
    so this GET call might be redundant unless specifically needed for other logic here.
    """
    if not FASTAPI_BASE_URL or not API_AUTH_TOKEN:
        return {"error": "Lambda configuration missing (URL or Token)", "statusCode": 500, "data": None}

    url = f"{FASTAPI_BASE_URL}/vocabularies/{vocabulary_id}/"
    headers = {'Authorization': f'Bearer {API_AUTH_TOKEN}'}

    logger.info(f"Calling GET {url}")
    async with session.get(url, headers=headers) as response:
        response_text = await response.text()
        logger.info(f"FastAPI GET Vocabulary response status: {response.status}, body: {response_text}")
        try:
            response_json = json.loads(response_text)
        except json.JSONDecodeError:
            response_json = {"raw_response": response_text}

        return {
            "statusCode": response.status,
            "data": response_json if response.status == 200 else None
        }

async def create_association_in_fastapi(vocabulary_id: int, session: aiohttp.ClientSession):
    """
    Calls the FastAPI endpoint to create an association.
    The FastAPI endpoint will handle AI generation and database storage.
    """
    if not FASTAPI_BASE_URL or not API_AUTH_TOKEN:
        return {"error": "Lambda configuration missing (URL or Token)", "statusCode": 500}

    url = f"{FASTAPI_BASE_URL}/associations/"
    # The payload for FastAPI's create_association is just the vocabulary_id
    payload = {'vocabulary_id': vocabulary_id}
    headers = {
        'Authorization': f'Bearer {API_AUTH_TOKEN}', 
        'Content-Type': 'application/json'
    }

    logger.info(f"Calling POST {url} with payload: {payload}")
    async with session.post(url, json=payload, headers=headers) as response:
        response_text = await response.text()
        logger.info(f"FastAPI POST Association response status: {response.status}, body: {response_text}")
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
        vocabulary_id = event_body.get('vocabulary_id')
        if vocabulary_id is None: # Check for None explicitly as 0 could be a valid ID
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing 'vocabulary_id' in request body"})
            }
        
        async with aiohttp.ClientSession() as session:
            vocab_details_response = await get_vocabulary_from_fastapi(vocabulary_id, session)
            if vocab_details_response["statusCode"] != 200:
                logger.warning(f"Failed to fetch vocabulary {vocabulary_id} or it doesn't exist. Status: {vocab_details_response['statusCode']}")
                # Decide if you want to proceed or return an error.
                # For this example, we'll proceed, letting the POST /associations/ handle final validation.
                # If you must stop, return:
                # return {
                #     "statusCode": vocab_details_response["statusCode"],
                #     "body": json.dumps({"error": "Failed to validate vocabulary_id", "details": vocab_details_response.get("data")})
                # }
                pass # Proceeding to let the POST endpoint handle it.

            association_result = await create_association_in_fastapi(vocabulary_id=vocabulary_id, session=session)
            return association_result

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