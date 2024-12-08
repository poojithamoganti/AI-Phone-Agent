import os
import json
import base64
import logging
import websockets
from websockets.client import WebSocketClientProtocol
from websockets.legacy.client import connect
from typing import Optional, AsyncGenerator, Tuple, Dict, List, Union
from pydantic import BaseModel
from datetime import datetime
from twilio.rest import Client
from pydub import AudioSegment
from io import BytesIO
import asyncio

from .models.schemas import ConfigModel, QuestionModel


logger = logging.getLogger(__name__)

try:
    from pydub import AudioSegment
    print("pydub imported successfully")
except ImportError as e:
    print(f"Error importing pydub: {e}")


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

        config = {
            "type": "session.update",
            "session": {
                "voice": "echo", 
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format" : "g711_ulaw",
                "modalities" : ["text", "audio"],
                "temperature": 0.8,
                "input_audio_transcription": {"model" : "whisper-1"}, 
                "instructions": (
                    "You are a helpful, witty, and friendly AI Assistant. "
                    "Your voice should be clear and engaging. "
                    "Keep your responses concise and natural for voice conversation. "
                    "Talk politely and professionally. "
                    "Always respond with both text and voice output."
                ),
            },
        }

        await self.openai_ws.send(json.dumps(config))
        logger.info("Sent detailed Session configuration")


    # Connecting to OpenAI real time api using websockets
    async def connect_to_openai(self):
        """Connect to OpenAI's Real Time API"""

        try:
            logger.info("Attempting to connect to OpenAI")
            api_key = os.getenv('OPENAI_API_KEY')
            uri = "wss://api.openai.com/v1/realtime"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Beta": "realtime=v1"
            }
            
            # Use connect instead of websockets.connect
            try:
                self.openai_ws = await asyncio.wait_for(
                    connect(
                        f"{uri}?model=gpt-4o-realtime-preview-2024-10-01",
                        extra_headers=headers
                    ),
                    timeout=10.0  # 10-second timeout
                )
            except asyncio.TimeoutError:
                logger.error("OpenAI WebSocket connection timed out")
                raise
            
            logger.info("Connected to OpenAI")
                
            await self.send_session_config()
            logger.info("Sent session configuration")
            
        except Exception as e:
            logger.error(f"Failed to connect to OpenAI: {str(e)}")
            raise

    

    

    # Asynchronous function which chunks raw audio, convert it to base64 and sends it to a buffer using events
    async def process_audio(self, audio_chunk: bytes)-> AsyncGenerator[str, None]:
        """Process Audio chunk using OpenAI's Real Time Speech API"""

        try:
            # Verify buffer size (8kHz * 0.1s = 800 bytes minimum)
            
            self.current_buffer.extend(audio_chunk)
            buffer_size = len(self.current_buffer)
            if buffer_size >= 800:
                base64_audio = base64.b64encode(bytes(self.current_buffer)).decode('utf-8')
                

                event_data = {
                "type": "input_audio_buffer.append",
                "audio": base64_audio
                }
                
                await self.openai_ws.send(json.dumps(event_data))
            
            
                # Send commit event
                commit_event = {"type": "input_audio_buffer.commit"}
                await self.openai_ws.send(json.dumps(commit_event))
                
                logger.info(f"Successfully sent and committed audio chunk of {buffer_size} bytes")

                self.current_buffer.clear()

            else:
                logger.debug(f"Accumulating audio data. Current buffer size: {buffer_size} bytes")

        except Exception as e:
            logger.error(f"Error while processing the audio: {e}")
            raise


    def convert_audio_to_twilio_format(self, audio_data: bytes) -> bytes:
        """Convert audio data to Twilio-compatible format"""
        try:
            audio_segment = AudioSegment(
                data=audio_data,
                sample_width=2,  # 16-bit
                frame_rate=48000,  # 48kHz
                channels=1  # mono
            )
            
            # Convert to 8kHz mu-law
            converted = audio_segment.export(
                format='raw',
                parameters=[
                    '-ar', '8000',  # Sample rate
                    '-ac', '1',     # Channels
                    '-acodec', 'pcm_mulaw'  # Codec
                ]
            )
            
            return converted.read()
            
        except Exception as e:
            logger.error(f"Audio conversion error: {e}")
            return audio_data  # Return original data if conversion fails

    # Asynchronous function which yields text and audio , returns either element could be None
    async def generate_voice_response(self) -> AsyncGenerator[Tuple[Optional[str], Optional[bytes]], None]:
        """Generate and Synthesize AI response"""
        try:
            current_text = ""
            
            async for message in self.openai_ws:
                event = json.loads(message)
                
                # Add more detailed logging
                logger.info(f"Received event type: {event.get('type')}")
                
                if event["type"] == "error":
                    logger.error(f"OpenAI error: {event['error']}")
                    yield "I apologize, I'm having trouble at this moment.", None
                    continue
                
                if event["type"] == "response.output_text.delta":
                    text = event["delta"]
                    current_text += text
                    self.current_buffer.append(text)
                    yield text, None
                    logger.info(f"Text delta received: {text}")
                
                elif event["type"] == "response.output_audio.delta":
                    audio_data = base64.b64decode(event["delta"])
                    converted_audio = self.convert_audio_to_twilio_format(audio_data)
                    yield None, converted_audio
                    logger.info(f"Audio delta received: {len(converted_audio)} bytes")
                
                elif event["type"] == "response.ended":
                    if current_text:
                        self.conversation_history.append({
                            "role": "assistant",
                            "content": current_text
                        })
                    current_text = ""
                    self.current_buffer = []
                    break
                
                elif event["type"] == "input_audio_buffer.speech_stopped":
                    if "text" in event:
                        self.conversation_history.append({
                            "role": "user",
                            "content": event["text"]
                        })
            
        except Exception as e:
            logger.error(f"Error while generating response: {e}")
            yield "I apologize, I'm having trouble at this moment.", None

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
                f.write(f"Conversation Transcript\n")
                f.write("================================\n\n")

                for msg in self.conversation_history:
                    f.write(f"{str(msg['role']).title()}: {msg['content']}\n\n")

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

    