import os 
import json
from dotenv import load_dotenv
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from datetime import datetime
from src.agent import PhoneAgent
from typing import Dict, Union, Optional
from src.models.schemas import ConfigModel
import uvicorn
from pyngrok import ngrok
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import base64
from starlette.websockets import WebSocketState
import websockets
from websockets import exceptions as ws_exceptions
import asyncio
import sys


load_dotenv()

ngrok.set_auth_token(os.getenv('NGROK_AUTH_TOKEN'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s  - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Setting up Fast API
app = FastAPI()

@app.get("/")
async def root():
    return {
        "message": "AI Phone Agent API is running",
        "status": "active",
        "time": str(datetime.now())
    }

@app.get("/test")
async def test_endpoint():
    """Test endpoint to verify server is accessible"""
    return {
        "status": "ok",
        "message": "Server is running and accessible",
        "ngrok_url": os.getenv('BASE_URL'),
        "timestamp": str(datetime.now())
    }

# Real time audio stream handling using websockets
@app.websocket("/stream")
async def handle_stream(websocket: WebSocket):
    print("WebSocket endpoint hit")
    logger.info("WebSocket connection attempt received")

    agent = None
    call_sid = None
    stream_sid = None
    media_buffer = bytearray()
    
    try:
        await websocket.accept()
        logger.info("WebSocket connection accepted")

        # Initialize agent and connect to OpenAI
        agent = PhoneAgent(os.getenv('CONFIG_PATH'))
        agent.call_start_time = datetime.now()
        logger.info("Agent initialized")
        await agent.connect_to_openai()
        logger.info("Connected to OpenAI")

        while True:
            try:
                message = await websocket.receive()
                
                # Handle disconnect message
                if isinstance(message, dict) and 'type' in message:
                    if message['type'] == 'websocket.disconnect':
                        logger.info("Received websocket disconnect message")
                        break

                # Handle text messages containing JSON
                if 'text' in message:
                    try:
                        json_data = json.loads(message['text'])
                        
                        if 'event' not in json_data:
                            continue

                        event_type = json_data['event']
                        
                        # Handle connection setup events
                        if event_type == 'connected':
                            logger.info("Media stream connected")
                            continue
                            
                        elif event_type == 'start':
                            try:
                                stream_sid = json_data['streamSid']
                                start_data = json_data['start']
                                call_sid = start_data['callSid']
                                logger.info(f"Media stream started - Call SID: {call_sid}, Stream SID: {stream_sid}")
                            except KeyError as e:
                                logger.error(f"Missing field in start event: {e}")
                            continue

                        # Handle media chunks
                        elif event_type == 'media':
                            try:
                                media_data = json_data['media']
                                payload = media_data['payload']

                                # Decode and buffer the audio
                                media_chunk = base64.b64decode(payload)
                                media_buffer.extend(media_chunk)

                                # Process the audio
                                await agent.process_audio(media_chunk)

                                # Generate and send response
                                async for text, audio in agent.generate_voice_response():
                                    if audio:
                                        await websocket.send_bytes(audio)
                                        logger.info("Sent audio response")
                                    if text:
                                        await websocket.send_json({"type": "transcript", "text": text})
                                        logger.info(f"Sent text: {text}")

                            except KeyError as e:
                                logger.error(f"Missing field in media event: {e}")
                            except Exception as e:
                                logger.error(f"Error handling media event: {e}")



                                
                        # Handle stop event
                        elif event_type == 'stop':
                            logger.info("Received stop event")
                            try:
                                stop_data = json_data['stop']
                                call_sid = stop_data['callSid']
                                logger.info(f"Call ending - Call SID: {call_sid}")
                                
                                # Process any remaining audio
                                if media_buffer:
                                    await agent.process_audio(bytes(media_buffer))
                                    media_buffer.clear()
                            except KeyError as e:
                                logger.error(f"Missing field in stop event: {e}")
                            except Exception as e:
                                logger.error(f"Error processing final buffer: {e}")
                            return  # Exit cleanly on stop event

                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse JSON: {e}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        continue

            except WebSocketDisconnect:
                logger.info("WebSocket disconnected")
                break
            except RuntimeError as e:
                if "disconnect message has been received" in str(e):
                    logger.info("Clean disconnect received")
                    break
                raise
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                break

    except Exception as e:
        logger.error(f"Error in WebSocket handler: {e}")
        logger.exception("Full error trace:")
    finally:
        # Cleanup
        if agent and agent.openai_ws:
            try:
                await agent.openai_ws.close()
                logger.info("Closed OpenAI connection")
                if call_sid:  # Only save if we have a call_sid
                    await agent.save_conversation(call_sid)
                    logger.info(f"Saved conversation for call {call_sid}")
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")

@app.post("/incoming-call")
async def handle_inbound_call():

    """Handle Incomming calls"""

    base_url = os.getenv('BASE_URL').replace('https://', '').replace('http://', '')
    

    response =VoiceResponse()

    response.say("Please wait while we connect your call to the A.I. voice assistant.")
    response.pause(length=1)
    connect = Connect()
    connect.stream(url=f"wss://{base_url}/stream", headers={
        "mode": "voice"
    })
    response.append(connect)
   
    twiml = str(response)
    logger.info(f"Generated TwiML for incomming call: {twiml}")
            
    return Response(content=twiml, media_type="application/xml")

 






@app.post("/outbound-call")
async def initiate_outbound_call(phone_number: str):
    try:
        # Load config path from environment or use default
        config_path = os.getenv('CONFIG_PATH', 'config/config.json')
        logger.info(f"Using config path: {config_path}")

        # Load configuration
        with open(config_path, 'r') as f:
            config_data = json.load(f)
     
        # Create ConfigModel from loaded data
        config_model = ConfigModel(**config_data)

        # Create PhoneAgent with config model
        agent = PhoneAgent(config_model)

        # Clean and properly format the base URL
        base_url = os.getenv("BASE_URL")
        if not base_url:
            raise ValueError("BASE_URL environment variable is not set")
        
        # Remove 'https://' if present and any Ngrok tunnel prefixes
        base_url = base_url.replace('https://', '').replace('NgrokTunnel: "', '').replace('"', '').split(' -> ')[0]
        
        # Construct a clean WebSocket URL
        webhook_url = f"wss://{base_url}/stream"
        
        logger.info(f"Cleaned Webhook URL for outbound call: {webhook_url}")

        # Create clean TwiML
        twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Connecting to AI voice assistant</Say>
    <Connect>
        <Stream url="{webhook_url}">
            <Parameter name="mode" value="voice"/>
        </Stream>
    </Connect>
    
</Response>'''
        
        logger.info(f"Generated TwiML for outbound call: {twiml}")

        # Initiate the call
        call = agent.twilio_client.calls.create(
            to=phone_number,
            from_=os.getenv('TWILIO_PHONE_NUMBER'),
            twiml=twiml
        )

        logger.info(f"Call initiated with SID: {call.sid}")

        return {"status": "success", "call_sid": call.sid}
    
    except Exception as e:
        logger.error(f"Error while initiating the call: {e}")
        return {"status": "error", "message": str(e)}
    

    
def ngrok_setup():
    ngrok_tunnel = ngrok.connect(8000, bind_tls=True)
    
    # Extract the clean public URL
    public_url = str(ngrok_tunnel).replace('NgrokTunnel: "', '').replace('"', '').split(' -> ')[0]
    
    # Set the environment variable
    os.environ['BASE_URL'] = public_url
    print(f'Public URL: {public_url}\n')
    return public_url

async def verify_openai_api_key():
    api_key = os.getenv('OPENAI_API_KEY')
    
    # Check if API key exists and has correct format
    if not api_key:
        raise ValueError("OpenAI API key not found. Please check your .env file.")
    if not api_key.startswith('sk-'):
        raise ValueError("Invalid API key format. Should start with 'sk-'")

    # Test connection to OpenAI real-time API
    uri = "wss://api.openai.com/v1/realtime"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1"
    }

    try:
        logger.info("Testing OpenAI API key...")
        async with websockets.connect(  # Use websockets.connect instead of just connect
            f"{uri}?model=gpt-4-0-realtime-preview-2024-10-01",
            extra_headers=headers  # Changed back to extra_headers
        ) as ws:
            logger.info("API key verified successfully!")
            return True
    except ws_exceptions.InvalidStatusCode as e:
        if e.status_code == 401:
            raise ValueError("Invalid API key or unauthorized access")
        elif e.status_code == 403:
            raise ValueError("API key doesn't have access to real-time API")
        else:
            raise ValueError(f"Connection failed with status code: {e.status_code}")
    except Exception as e:
        raise ValueError(f"Failed to verify API key: {str(e)}")

if __name__ == "__main__":
    try:
        # Verify API key before starting server
        asyncio.run(verify_openai_api_key())
        
        public_url = ngrok_setup()
        print(f"Ngrok URL: {public_url}\n")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except Exception as e:
        print(f"Startup failed: {str(e)}")
        sys.exit(1)





               

