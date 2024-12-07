import os 
import json
from dotenv import load_dotenv
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from datetime import datetime
from src.agent import PhoneAgent
from typing import Dict, Union, Optional
from src.models.schemas import ConfigModel
import uvicorn
from pyngrok import ngrok

load_dotenv()

ngrok.set_auth_token(os.getenv('NGROK_AUTH_TOKEN'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s  - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Setting up Fast API
app = FastAPI()

@app.get("/")
async def root():
    return {"message" : "AI Phone Agent API is running!!!"}

# Real time audio stream handling using websockets
@app.websocket("/stream")
async def handle_stream(websocket: WebSocket):

    """Handle real-time audio stream"""
    await websocket.accept()
    logger.info("WebSocket connection accepted")

    agent = Optional[PhoneAgent] = None
    call_sid = websocket.headers.get("Twilio-Call-SID")
    logger.info("WebSocket connection accepted")


    try:

        agent = PhoneAgent(os.getenv('CONFIG_PATH'))
        agent.call_start_time =datetime.now()
        logger.info("Agent Intialized")

        await agent.connect_to_openai()
        logger.info("Connected to OpenAI")

        while True:

            data = await websocket.receive_bytes()
            logger.info(f"Received audio chunk size: {len(data)}")

            await agent.process_audio(data)


            async for text,audio in agent.generate_voice_response():
                if audio:
                    await websocket.send_bytes(data)

                if text:
                    await websocket.send_json({
                        "type" : "transcript",
                        "text": text
                    })

    except WebSocketDisconnect:
        logger.info(f"Call ended: {call_sid}")

    except Exception as e:
        logger.error(f"Error while connecting to Websocket: {e}")
    finally:
        if agent:
            if agent.openai_ws:
                await agent.openai_ws.close()
            await agent.save_conversation(call_sid)




@app.post("/outbound-call")
async def initiate_outbound_call(phone_number: str):

    """Intiate an Outbound call"""

    try:
        config_path =os.getenv('CONFIG_PATH' , 'config/config.json')
        logger.info(f"Using config path: {config_path}")

        base_url = os.getenv("BASE_URL")
        logger.info(f"BASE_URL from env: {base_url}")

        with open(config_path, 'r') as f:
            config_data = json.load(f)

     
        config_model = ConfigModel(**config_data)

        agent = PhoneAgent(config_model)

        webhook_url = f"wss://{base_url}/stream"
        logger.info(f"WebSocket URL Being usde: {webhook_url}")

        twiml= f'''
            <Response>
                <Connect>
                    <Stream url = "{webhook_url}"/>
                        <Parameter name="mode" value="voice" />
                    </Stream>
                </Connect>
                <Pause length="120 />
            </Response>

            '''
        logger.info(f"Generated TwiML: {twiml}")

        call = agent.twilio_client.calls.create(
            to = phone_number,
            from_= os.getenv('TWILIO_PHONE_NUMBER'),
            twiml= twiml
            
        )

        logger.info(f"Call initiated with SID: {call.sid}")

        return {"status": "success", "call_sid": call.sid}
    
    except Exception as e:
        logger.error(f"Error while initiating the call: {e}")

        return {"status": "error", "message": str(e)}
    

def ngrok_setup():
    url = ngrok.connect(8000)
    print(f'Public URL: {url}\n')
    return url

if __name__ == "__main__":
    public_url = ngrok_setup()
    print(f"Ngrok URL: {public_url}\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)







               

