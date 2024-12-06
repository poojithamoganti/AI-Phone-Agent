import os 
import json
from typing import Dict, List, Optional, Union, AsyncGenerator
from dotenv import load_dotenv
from pydantic import BaseModel
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Start, Connect
import logging
import websockets
import base64
from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s  - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# To Define correct structure for each question in the config file
class QuestionModel(BaseModel):
    topic: str
    question: str

# To Define correct structure to the whole config file to make sure it contains system prompt and a list of questions
class ConfigModel(BaseModel):
    system: str
    questions: List[QuestionModel]


class PhoneAgent:

    # To load config.json as a string or a configmodel object
    def __init__(self, config: Union[ConfigModel , str]):
        self.config = self._load_config(config) if isinstance(config, str) else config
        # OpenAi websocket
        self.openai_ws = None

        # Twilio client instance with authentication
        self.twilio_client = Client(
            os.getenv('TWILIO_ACCOUNT_SID'),
            os.getenv('TWILIO_AUTH_TOKEN')
        )

        self.call_start_time = None

        # List to store entire conversation
        self.conversation_history= []

        self.current_buffer =[]


       
        # To read the config.json file and load the configuration
        def _load_config(self, config_path:str) -> ConfigModel:

            """Load configuration from file"""

            try:
                with open(config_path, 'r') as f:
                    return ConfigModel(**json.load(f))
                
            except Exception as e:
                logger.error(f"Error Loading config file: {e}")
                raise

        # Connecting to OpenAI real time api using websockets
        async def connect_to_openai(self):
            """Connect to OpenAI's Real Time API"""

            try:
                uri= "wss://api.openai.com/v1/realtime"
                headers ={
                    "Authorization" : f"Bearer {os.getenv('OPENAI_API_KEY')}",
                    "OpenAI-Beta": "realtime=v1"
                }

                self.openai_ws = await websockets.connect(
                    f"{uri}?model=gpt-4o-realtime-preview-2024-10-01",
                    extra_headers=headers
                )

                
                logger.info("Connected to OpenAI RealTime API")

            except Exception as e:
                logger.error(f"Error while connecting to OpenAI RealTime API: {e}")
                raise



        # Asynchronous function which chunks raw audio, convert it to base64 and sends it to a buffer
        async def process_audio(self, audio_chunk: bytes)-> AsyncGenerator[str, None]:
            """Process Audio chunk using OpenAI's Real Time Speech API"""

            try:

                base64_audio =base64.b64encode(audio_chunk).decode('utf-8')

                event_append ={
                    "type": "input_audio_buffer.append",
                    "audio": base64_audio
                }

                await self.openai_ws.send(json.dumps(event_append))

                event_commit = {"type": "input_audio_buffer.commit"}
                await self.openai.ws.send(json.dumps(event_commit))

                event_response = {"type": "input_audio_buffer.commit"}
                await self.openai.ws.send(json.dumps(event_response)) 
            
            except Exception as e:
                logger.error(f"Error while processing the audio: {e}")
                yield ""


        # Asynchronous function which takes transcribed text as input and returns text and audio bytes as tuple
        async def generate_voice_response(self, transcript: str)-> tuple[str, bytes]:

            """Generate and Synthesize AI response"""

            #Generating response using GPT-4 turbo for reasoning
            try:

                messages =[
                        {"role": "system", "content": self.config.system},
                        {"role": "system", "content": "Keep reponses concise and natural for voice conversation."},
                        {"role": "system", "content": "Talk Professional, cordial and conversationally fluid."}
                    ]
                
                for msg in self.conversation_history[-7:]:
                    messages.append({"role": "system", "content": msg}) 

                messages.append({"role": "user", "content": transcript})
                       


                chat_response = await self.openai_client.chat.completions.create(
                    model ="gpt-4-turbo-preview",
                    messages=messages,
                    temperature=0.7,
                    max_tokens=150
                    
                )

                response_text = chat_response.choices[0].message.content
                self.conversation_history.append(response_text)

                # Using OpenAI TTS to generate speech
                speech_response = await self.openai_client.audio.speech.create(
                    model="tts-1-hd",
                    voice="echo",
                    input=response_text,
                    speed =1

                )

                return response_text, speech_response.content 
            
            except Exception as e:
                logger.error(f"Error while generating response: {e}")
                return "I apologize, but I'm having trouble at this moment." ,b""

      


