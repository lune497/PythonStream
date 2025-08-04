import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
import uvicorn

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

with open("rapport_bnp_satisfaction_2024.txt", "r", encoding="utf-8") as f:
    document_contenu = f.read()



# SYSTEM_MESSAGE = (
#     "Tu es un assistant vocal téléphonique. "
#     "Tu ne dois répondre qu'en te basant exclusivement sur le CONTEXTE FIXE ci-dessous. "
#     "Même si tu connais la réponse grâce à ta base de données, tu dois refuser de répondre si l'information n'apparaît pas dans ce texte. "
#     "Ne complète jamais avec tes connaissances personnelles ou générales. "
#     "Si la réponse n'est pas présente dans le texte, dis simplement : "
#     "\"Je suis désolé, je ne suis pas autorisé à répondre à cela. Je vous invite à écrire à l'adresse : lavoixduclient@lvdc.fr.\"\n\n"
#     "=== CONTEXTE FIXE ===\n"
#     "Procédure d’ouverture de compte bancaire en FRANCE :\n"
#     "- L'utilisateur doit présenter une pièce d'identité valide (CNI, passeport ou titre de séjour).\n"
#     "- Un justificatif de domicile de moins de 3 mois est requis.\n"
#     "- Un dépôt minimum de 50 € est nécessaire à l’ouverture.\n"
#     "- Le délai moyen d’activation du compte est de 48 heures ouvrées.\n"
#     "- Le RIB est délivré immédiatement après la création du compte.\n"
# )
SYSTEM_MESSAGE = (
    "Tu es un expert chargé d’analyser un document fourni par l’utilisateur. "
    "Tu ne dois répondre qu'en te basant exclusivement sur le contenu du document ci-dessous. "
    "Même si tu connais la réponse grâce à ta base de données, tu dois refuser de répondre si l'information n'apparaît pas dans ce document."
    "Tu n’as pas le droit d’utiliser tes connaissances personnelles ou générales, ni d’inventer des réponses. "
    "Si une information n’est pas explicitement présente dans le document, tu dois dire poliment : "
    "\"Je suis désolé, je ne peux pas répondre à cette question car l'information ne figure pas dans le document fourni.\"\n\n"
    "=== DOCUMENT À ANALYSER ===\n"
    f"{document_contenu}"
)

VOICE = "alloy"

LOG_EVENT_TYPES = [
    "response.content.done", "rate_limits.updated", "response.done",
    "input_audio_buffer.committed", "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started", "session.created"
]

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY manquant dans le fichier .env")


@app.api_route("/", methods=["GET", "POST"])
async def index_page():
    return "<h1>✅ Serveur en ligne</h1>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    response = VoiceResponse()
    response.say("Veuillez patienter pendant que nous vous connectons à l'assistant vocal.")
    response.pause(length=1)
    response.say("OK, vous pouvez commencer à parler maintenant !")

    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)

    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    print("📞 Client connecté au WebSocket")
    await websocket.accept()

    async with websockets.connect(
        # 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        'wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        print("🔗 Connecté à OpenAI Realtime WebSocket")
        await send_session_update(openai_ws)

        stream_sid = None

        async def receive_from_twilio():
            nonlocal stream_sid
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    print(f"📥 Twilio > event: {data.get('event')}")

                    if data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"✅ Stream SID: {stream_sid}")

                    elif data['event'] == 'media' and openai_ws.open:
                        print("🎙️  Envoi audio vers OpenAI")
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }))

                    elif data['event'] == 'stop':
                        print("🛑 Twilio stream terminé")
                        break

            except WebSocketDisconnect:
                print("❌ WebSocket Twilio déconnecté")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal stream_sid
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    print(f"🧠 OpenAI event: {response['type']}")

                    if response['type'] == 'session.updated':
                        print("⚙️ Session mise à jour")

                    elif response['type'] == 'input_audio_buffer.speech_started':
                         print('Speech Start:', response['type'])
                         # Clear Twilio buffer
                         clear_twilio = {
                            "streamSid": stream_sid,
                            "event": "clear"
                         }
                         await websocket.send_json(clear_twilio)
                         print('Cleared Twilio buffer.')
                        
                        # Send interrupt message to OpenAI
                         interrupt_message = {
                            "type": "response.cancel"
                         }
                         await openai_ws.send(json.dumps(interrupt_message))
                         print('Cancelling AI speech from the server.')

                    elif response['type'] == 'input_audio_buffer.speech_stopped':
                        print("🔴 Fin de parole détectée")

                    elif response['type'] == 'input_audio_buffer.committed':
                        print("📦 Audio enregistré")

                    elif response['type'] == 'response.audio_transcript.done':
                        print("📝 Transcription complète :", response.get("transcript"))

                    elif response['type'] == 'response.content_part.done':
                        print("🧾 Contenu partiel :", response.get("content"))

                    elif response['type'] == 'response.output_item.done':
                        print("📦 Réponse structurée complète :", json.dumps(response, indent=2))

                    elif response['type'] == 'response.done':
                        print("✅ Génération terminée :", response)

                    elif response['type'] == 'response.audio.delta' and response.get('delta'):
                        print("🔊 Envoi de l'audio vers Twilio")
                        try:
                            audio_payload = base64.b64encode(
                                base64.b64decode(response['delta'])
                            ).decode('utf-8')
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": audio_payload}
                            })
                        except Exception as e:
                            print(f"❗ Erreur lors de l'envoi de l'audio : {e}")

            except Exception as e:
                print(f"❌ Erreur dans send_to_twilio : {e}")

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_session_update(openai_ws):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.3,
                "silence_duration_ms": 200,
                "create_response": True,
                "interrupt_response": True
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.7,
        }
    }
    print("⚙️ Session config envoyée:", json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)