from __future__ import annotations
import os
import json
import base64
import asyncio
import websockets
from typing import List, Dict, Optional
from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, Request, HTTPException, Body
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.base.exceptions import TwilioRestException
from pyngrok import ngrok
from dotenv import load_dotenv
import logging
from logging.handlers import RotatingFileHandler
import uvicorn
from datetime import datetime



# Load environment variables
load_dotenv()



# Pydantic models for validation
class QuestionModel(BaseModel):
    topic: str
    question: str

class ConfigModel(BaseModel):
    system: str
    questions: List[QuestionModel]




# Configure logging
def setup_logging():
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler
    os.makedirs('logs', exist_ok=True)
    file_handler = RotatingFileHandler(
        'logs/app.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)  # Set to DEBUG level
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # Set to DEBUG level
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logging()


# Load config.json
def load_config():
    with open('config.json', 'r') as f:
        return json.load(f)
    



CONFIG = load_config()
SYSTEM_MESSAGE = CONFIG['system']
VOICE = 'alloy'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created']


# Environment variables
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
NGROK_AUTH_TOKEN = os.getenv('NGROK_AUTH_TOKEN')



# Initialize FastAPI app
app = FastAPI()


# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


class PhoneAgent:
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.current_question_index = 0
        self.responses = {}
        self.transcript = []
        self.questions = CONFIG['questions']
        
       # Create base directory for transcripts
        self.base_dir = 'transcripts'
        os.makedirs(self.base_dir, exist_ok=True)
        
        # Create transcript file path using call_sid
        self.transcript_path = os.path.join(self.base_dir, f'{call_sid}.txt')
        
    
        
        # Initialize transcript file
        with open(self.transcript_path, 'w', encoding='utf-8') as f:
                f.write(f"AI Phone Agent Call Transcript\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Call SID: {call_sid}\n")
                f.write(f"Call Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write("Conversation:\n")
                f.write("-" * 20 + "\n\n")
        logger.info(f"Created a transcript file: {self.transcript_path}")




    async def add_to_transcript(self, speaker: str, message: str):
        if not message or not message.strip():
            return
            
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            entry = f"[{timestamp}] {speaker}: {message.strip()}\n"
            
            # Write to file immediately
            with open(self.transcript_path, 'a', encoding='utf-8') as f:
                f.write(entry)
            
            # Store in memory
            self.transcript.append(entry)
            logger.info(f"Added {speaker} message to transcript: {message[:50]}...")
        except Exception as e:
            logger.error(f"Error writing to transcript: {str(e)}")

    async def save_transcript(self):
        try:
            with open(self.transcript_path, 'a', encoding='utf-8') as f:
                f.write("\n" + "=" * 50 + "\n")
                f.write(f"Call Ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            logger.info(f"Saved final transcript for call {self.call_sid}")
        except Exception as e:
            logger.error(f"Error saving final transcript: {str(e)}")


    def get_next_question(self) -> Optional[str]:
        if self.current_question_index < len(self.questions):
            question = self.questions[self.current_question_index]
            return f"For {question['topic']}, please provide your {question['question']}."
        return None

    
 

class ConversationManager:
    def __init__(self):
        self.conversations: Dict[str, PhoneAgent] = {}
        self.lock = asyncio.Lock()

    async def add_conversation(self, call_sid: str) -> PhoneAgent:
        async with self.lock:
            if call_sid in self.conversations:
                return self.conversations[call_sid]
            
            phone_agent = PhoneAgent(call_sid)
            self.conversations[call_sid] = phone_agent
            logger.info(f"Added new conversation for call {call_sid}")
            return phone_agent

    async def remove_conversation(self, call_sid: str):
        async with self.lock:
            if call_sid in self.conversations:
                try:
                    phone_agent = self.conversations[call_sid]
                    # Save any final transcript
                    await phone_agent.save_transcript()
                    # Remove from dictionary
                    del self.conversations[call_sid]
                    logger.info(f"Successfully removed conversation for call {call_sid}")
                except Exception as e:
                    logger.error(f"Error during conversation cleanup: {str(e)}")


    async def get_conversation(self, call_sid: str) -> Optional[PhoneAgent]:
        async with self.lock:
            return self.conversations.get(call_sid)

                    
# Initialize conversation manager
conversation_manager = ConversationManager()

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return {"message": "AI Phone Agent is running!"}



@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    
    response.pause(length=1)
    response.say("Please wait while I connect you to A.I. Voice Agent. Hello!!!")
   
    host = request.url.hostname
    print(host)
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream?call_type=incoming')
    response.append(connect)
    logger.info("New Incomming call.........")
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/make-call")
async def make_call(phone_number: str = Body(..., embed=True)):
    """Make an outgoing call to the specified phone number."""
    if not phone_number:
        logger.error("Phone number is required")
        return {"error": "Phone number is required"}
    
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    base_url = os.getenv('BASE_URL')
    if not base_url:
        raise HTTPException(status_code=500, detail="BASE_URL not set")
    
    call = client.calls.create(
        url=f"{base_url}/outbound-call",  
        to=phone_number,   
        from_=TWILIO_PHONE_NUMBER
    )
    return {"call_sid": call.sid}
    

@app.api_route("/outbound-call", methods=["POST"])
async def make_outbound_call(request: Request):
    """Handle outgoing call and return TwiML response to connect to Media Stream"""
    
    logger.info(f"Initiating outbound call........")
        
    # Create TwiML for the call
    response = VoiceResponse()
    response.pause(length=1)  # Brief pause
    
    # Initial greeting
    response.say("Welcome to our legal AI Phone Agent. Please stay on the line while I connect you with our assistant.")
    response.pause(length=1)  # Give time for connection
    response.say("Hello")
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream?call_type=outbound')
    response.append(connect)
 
    logger.info(f"Outbound call initiated......")
    return HTMLResponse(content=str(response) , media_type="application/xml")
    
    

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):

    await websocket.accept()
    stream_sid = None
    phone_agent = None
    session_id = None

    
    logger.info(f"WebSocket connection accepted .............")

    
    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        },
        
    ) as openai_ws:
        logger.info("Connected to OpenAI WebSocket")
        
        # Send initial configuration
        await openai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": SYSTEM_MESSAGE,
                "modalities": ["text", "audio"],
                "temperature": 0.8,
            }
        }))
        logger.info("Sent initial configuration to OpenAI")
        
        async def receive_from_twilio():
            nonlocal stream_sid, phone_agent
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_agent = await conversation_manager.add_conversation(stream_sid)
                        logger.info(f"Started new conversation for stream {stream_sid}")
                    elif data['event'] == 'media' and openai_ws.open:
                        # Send audio buffer to OpenAI
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }))
            except WebSocketDisconnect:
                logger.info("WebSocket disconnected")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, session_id
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)
                    if response['type'] == 'session.created':
                        session_id = response['session']['id']
                    if response['type'] == 'session.updated':
                        print("Session updated successfully:", response)
                    if response['type'] == 'response.audio.delta' and response.get('delta'):
                        try:
                            audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                            audio_delta = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": audio_payload
                                }
                            }
                            await websocket.send_json(audio_delta)
                        except Exception as e:
                            print(f"Error processing audio data: {e}")
                    if response['type'] == 'conversation.item.created':
                        print(f"conversation.item.created event: {response}")
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())



def setup_ngrok():
    ngrok.set_auth_token(NGROK_AUTH_TOKEN)
    ngrok_tunnel = ngrok.connect(8000, bind_tls=True)
    public_url = str(ngrok_tunnel).replace('NgrokTunnel: "', '').replace('"', '').split(' -> ')[0]
    os.environ['BASE_URL'] = public_url
    logger.info(f"Ngrok URL: {public_url}")
    return public_url

if __name__ == "__main__":
    # Setup ngrok tunnel
    public_url = setup_ngrok()
    print(f"Server running at: {public_url}")

    # Run the FastAPI application
    uvicorn.run(app, host="0.0.0.0", port=8000)


