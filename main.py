from __future__ import annotations
import os
import json
import base64
import asyncio
import websockets
from typing import List, Dict, Optional
from pydantic import BaseModel
from fastapi import FastAPI, WebSocket, Request, HTTPException
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

class OutboundCallRequest(BaseModel):
    phone_number: str
    callback_url: Optional[str] = None


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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



# Initialize FastAPI app
app = FastAPI()

# Environment variables
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
NGROK_AUTH_TOKEN = os.getenv('NGROK_AUTH_TOKEN')

# Initialize Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)





class PhoneAgent:
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.current_question_index = 0
        self.responses = {}
        self.transcript = []
        self.questions = CONFIG['questions']
        self._create_empty_transcript()
        logger.info(f"New conversation started - Call SID: {call_sid}")


    def _create_empty_transcript(self):
        """Create an empty transcript file as soon as the conversation starts."""
        try:
            filename = f"transcripts/call_{self.call_sid}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write("Legal Intake Call Transcript\n")
                f.write("=" * 50 + "\n\n")
                f.write(f"Call Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("Status: Active\n\n")
                f.write("Conversation:\n")
                f.write("-" * 20 + "\n")
            logger.info(f"Created empty transcript file for call {self.call_sid}")
        except Exception as e:
            logger.error(f"Error creating empty transcript for call {self.call_sid}: {str(e)}")


    def add_to_transcript(self, speaker: str, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "timestamp": timestamp,
            "speaker": speaker,
            "message": message.strip()
        }
        self.transcript.append(entry)
        filename = f"transcripts/call_{self.call_sid}.txt"
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"[{entry['timestamp']}] {entry['speaker']}: {entry['message']}\n")
        logger.info(f"Added to transcript - {speaker}: {message.strip()}")

    def get_next_question(self) -> Optional[str]:
        if self.current_question_index < len(self.questions):
            question = self.questions[self.current_question_index]
            return f"For {question['topic']}, please provide your {question['question']}."
        return None

    def save_transcript(self):
        try:
            filename = f"transcripts/call_{self.call_sid}.txt"
            
            # Read existing content
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    existing_content = f.read()
            except FileNotFoundError:
                existing_content = "Legal Intake Call Transcript\n" + "=" * 50 + "\n\n"
            
            # Create new content
            with open(filename, 'w', encoding='utf-8') as f:
                # Write header if it's not already there
                if not existing_content.startswith("Legal Intake Call"):
                    f.write("Legal Intake Call Transcript\n")
                    f.write("=" * 50 + "\n\n")
                
                # Write call end time
                f.write(f"Call Ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("Status: Completed\n\n")
                
                # Write conversation
                f.write("Conversation:\n")
                f.write("-" * 20 + "\n")
                for entry in self.transcript:
                    f.write(f"[{entry['timestamp']}] {entry['speaker']}: {entry['message']}\n")
                
                # Write collected information
                f.write("\n\nCollected Information:\n")
                f.write("-" * 20 + "\n")
                for question in self.questions:
                    key = f"{question['topic']}_{question['question']}"
                    response = self.responses.get(key, "Not provided")
                    f.write(f"\n{question['topic']} - {question['question']}:\n{response}\n")
            
            logger.info(f"Final transcript saved to: {filename}")
            
        except Exception as e:
            logger.error(f"Error saving final transcript: {str(e)}")
            # Try one last time to save at least some information
            try:
                with open(filename, 'a', encoding='utf-8') as f:
                    f.write(f"\n\nError saving full transcript: {str(e)}\n")
                    f.write(f"Call ended at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            except Exception as backup_error:
                logger.error(f"Critical error saving transcript: {str(backup_error)}")


class ConversationManager:
    def __init__(self):
        self.conversations: Dict[str, PhoneAgent] = {}
        self.lock = asyncio.Lock()
        logger.info("Conversation Manager initialized")

    async def add_conversation(self, call_sid: str) -> PhoneAgent:
        async with self.lock:
            if call_sid in self.conversations:
                logger.info(f"Returning existing conversation for call {call_sid}")
                return self.conversations[call_sid]
            
            phone_agent = PhoneAgent(call_sid)
            self.conversations[call_sid] = phone_agent
            logger.info(f"Added new conversation for call {call_sid}")
            return phone_agent

    async def remove_conversation(self, call_sid: str):
        async with self.lock:
            if call_sid in self.conversations:
                try:
                    conversation = self.conversations[call_sid]
                    conversation.save_transcript()
                    del self.conversations[call_sid]
                    logger.info(f"Removed conversation and saved transcript for call {call_sid}")
                except Exception as e:
                    logger.error(f"Error during conversation cleanup: {str(e)}")
                    # Try one last time to save
                    try:
                        filename = f"transcripts/call_{call_sid}.txt"
                        with open(filename, 'a', encoding='utf-8') as f:
                            f.write(f"\n\nError during cleanup: {str(e)}\n")
                            f.write(f"Call forcefully ended at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    except Exception as backup_error:
                        logger.error(f"Critical error during final save attempt: {str(backup_error)}")

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
    response.say("Please wait while I connect you to A.I. Voice Agent.")
   
    host = request.url.hostname
    print(host)
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    logger.info("New Incomming call.........")
    return HTMLResponse(content=str(response), media_type="application/xml")




@app.post("/outbound-call")

async def make_outbound_call(call_request: OutboundCallRequest):
    try:
        logger.info(f"Initiating outbound call to {call_request.phone_number}")
        
        # Create TwiML for the outbound call
        response = VoiceResponse()
        response.pause(length=1)
        response.say("Welcome to our legal intake line. I'll be assisting you today.")
        
        # Get the ngrok URL from environment
        base_url = os.getenv('BASE_URL')
        if not base_url:
            raise HTTPException(status_code=500, detail="BASE_URL not set")
        
        connect = Connect()
        connect.stream(url=f'wss://{base_url}/media-stream')
        response.append(connect)
        
        # Make the call using Twilio
        call = twilio_client.calls.create(
            to=call_request.phone_number,
            from_=TWILIO_PHONE_NUMBER,
            twiml=str(response)
        )
        
        logger.info(f"Outbound call initiated - SID: {call.sid}")
        return {"message": "Call initiated", "call_sid": call.sid}
        
    except Exception as e:
        logger.error(f"Error making outbound call: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/call-status")
async def call_status_callback(request: Request):
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        status = form_data.get('CallStatus')
        
        logger.info(f"Call status update - SID: {call_sid}, Status: {status}")
        
        if status in ['completed', 'failed', 'busy', 'no-answer', 'canceled']:
            await conversation_manager.remove_conversation(call_sid)
        
        return {"status": "success"}
        
    except Exception as e:
        logger.error(f"Error in call status callback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    stream_sid = None
    phone_agent = None

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        # Send initial session configuration
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

        async def receive_from_twilio():
            nonlocal stream_sid, phone_agent
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        phone_agent = await conversation_manager.add_conversation(stream_sid)
                    elif data['event'] == 'media' and openai_ws.open:
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }))
            except WebSocketDisconnect:
                if stream_sid:
                    await conversation_manager.remove_conversation(stream_sid)

        async def send_to_twilio():
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    
                    if response['type'] == 'response.content.part':
                        content = response.get('content', '')
                        if phone_agent and content.strip():
                            phone_agent.add_to_transcript('Assistant', content)
                    
                    elif response['type'] == 'input_audio_buffer.speech_started':
                        text = response.get('text', '')
                        if phone_agent and text.strip():
                            phone_agent.add_to_transcript('User', text)
                            
                            if phone_agent.current_question_index < len(phone_agent.questions):
                                current_q = phone_agent.questions[phone_agent.current_question_index]
                                key = f"{current_q['topic']}_{current_q['question']}"
                                phone_agent.responses[key] = text
                                phone_agent.current_question_index += 1
                                
                                next_question = phone_agent.get_next_question()
                                if next_question:
                                    await openai_ws.send(json.dumps({
                                        "type": "message",
                                        "content": next_question
                                    }))
                                else:
                                    await openai_ws.send(json.dumps({
                                        "type": "message",
                                        "content": "Thank you for providing all the information. Is there anything else you'd like to add before we end the call?"
                                    }))
                    
                    elif response['type'] == 'response.audio.delta' and response.get('delta'):
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        await websocket.send_json({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        })
                        
            except Exception as e:
                logger.error(f"Error in send_to_twilio: {e}")
                if stream_sid:
                    await conversation_manager.remove_conversation(stream_sid)

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


