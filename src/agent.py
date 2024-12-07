import os
import json
import base64
import logging
import websockets
from websockets.client import WebSocketClientProtocol
from typing import Optional, AsyncGenerator, Tuple, Dict, List, Union
from pydantic import BaseModel
from datetime import datetime
from twilio.rest import Client

from .models.schemas import ConfigModel, QuestionModel


logger = logging.getLogger(__name__)


class PhoneAgent:

    # To load config.json as a string or a configmodel object
    def __init__(self, config: Union[ConfigModel , str]):
        self.config = self._load_config(config) if isinstance(config, str) else config
        # OpenAi websocket
        self.openai_ws : Optional[WebSocketClientProtocol]= None

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

# To send session configuration information, give instructions, provide transcriptions and get additional information
        async def send_session_config(self):

            "Send Initial session configuration"

            config ={
                "type": "session.update",
                "session": {
                    "voice": "echo",
                    "turn_detection": "server_vad",
                    "input_audio_transcription": True,
                    "instructions": (
                        "You are a helpful, witty, and friendly AI Assistant."
                        "Your voice and personality should be warm and engaging."
                        "Keep your responses concise and natural for voice conversation."
                        "Talk politely and be as Professional as you can."
                        "If more information is required, ask short and clear questions."
                        
                    ),
                    # To get additional information regarding the climatic conditions where the incident took place
                    "tools": [{
                        "name": "get_weather",
                        "description": "Get location's weather information",
                        "parameters": {
                            "type": "object",
                            "properties" : {
                                "location" : {"type": "string"}
                            }
                        }

                    }]
                }
            }

            await self.openai_ws.send(json.dumps(config))
            logger.info("Sent Session configuration")


        # To send session configuration information, give instructions, provide transcriptions and get additional information
        async def send_session_config(self):

            "Send Initial session configuration"

            config ={
                "type": "session.update",
                "session": {
                    "voice": "echo",
                    "turn_detection": "server_vad",
                    "input_audio_transcription": True,
                    "instructions": (
                        "You are a helpful, witty, and friendly AI Assistant."
                        "Your voice and personality should be warm and engaging."
                        "Keep your responses concise and natural for voice conversation."
                        "Talk politely and be as Professional as you can."
                        "If more information is required, ask short and clear questions."
                        
                    ),
                    # To get additional information regarding the climatic conditions where the incident took place
                    "tools": [{
                        "name": "get_weather",
                        "description": "Get location's weather information",
                        "parameters": {
                            "type": "object",
                            "properties" : {
                                "location" : {"type": "string"}
                            }
                        }

                    }]
                }
            }

            await self.openai_ws.send(json.dumps(config))
            logger.info("Sent Session configuration")




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

                await self.send_session_config()
                logger.info("Connected to OpenAI RealTime API")

            except Exception as e:
                logger.error(f"Error while connecting to OpenAI RealTime API: {e}")
                raise


        

        # Asynchronous function which chunks raw audio, convert it to base64 and sends it to a buffer using events
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
                raise


        # Asynchronous function which yields text and audio , returns either element could be None
        async def generate_voice_response(self)-> AsyncGenerator[Tuple[Optional[str], Optional[bytes]], None]:

            """Generate and Synthesize AI response"""

            #Generating response using GPT-4 turbo for reasoning
            try:

                current_text =""

                # To convert websocket message to json
                async for message in self.openai_ws:
                    event = json.loads(message)

                if event["type"] == "error":
                    logger.error(f"OpenAI error: {event['error']}")
                    yield "I apologize, I'm having trouble at this moment.", None
                   
                
                if event["type"] == "response.output_text.delta":
                    text = event["delta"]
                    current_text += text
                    self.currrent_buffer.append(text)
                    yield text, None

                elif event["type"] == "response.output_audio.delta":
                    audio_data = base64.b64decode(event["delta"])
                    yield None, audio_data

                elif event["type"] == "response.ended":
                    if current_text:
                        self.conversation_history.append({
                            "role": "assistant",
                            "content" : current_text

                        })

                        current_text= ""
                        self.current_buffer  =[]
                        
                elif event["type"] == "input_audio_buffer.stopped":
                    if "text" in event:
                        self.conversation_history.append({
                            "role": "user",
                            "content": event["text"]
                        })

            except Exception as e:
                logger.error(f"Error while generating response: {e}")
                yield " I apologize, I'm having trouble at this moment." , None



        # To save the conversation in a given format with questions and answers
        async def save_conversation(self, call_sid: str):

            """Save Conversation Transcript and metadata"""

            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                results_dir = f"calls/{call_sid}_{timestamp}"
                os.makedirs(results_dir, exist_ok =True)

                conversation_summary ={
                    "call_id" : call_sid,
                    "timestamp": timestamp,
                    "duration" : self._calculate_call_duration(),
                    "transcript": self.conversation_history
                }

                # Saving the transcipt of the call as a text file
                with open(f"{results_dir}/transcript.txt", 'w') as f:
                    f.write(f"Call Summary\n")
                    f.write(f"===================================\n")
                    f.write(f"Call ID: {call_sid}\n")
                    f.write(f"Date: {timestamp}\n")
                    f.write(f"Duration: {conversation_summary['duration']}\n\n")
                    f.write(f"Cpnversation Transcript\n")
                    f.write("================================\n\n")

                    for msg in self.conversation_history:
                        f.write(f"{msg['role'].title()} : {msg['content']}\n\n")

                logger.info(f"Conversation saved to {results_dir}")
                return conversation_summary
            
            except Exception as e:
                logger.error(f"Error while saving the conversation: {e}")
                raise


        def _calculate_call_duration(self)->str:

            """Calculate Call Duration"""

            if not self.call_start_time:
                return "N/A"
            return str(datetime.now() - self.call_start_time)
