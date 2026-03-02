import asyncio
from pyrogram import Client

API_ID = 35315188
API_HASH = 'ccf9a114d0b6401bddec3f0aa243a029'
BOT_TOKEN = '8762932401:AAHoWrdYm8fhIt2e1RB-qktQhc5gFFa1ONQ'
CHANNEL_USERNAME = '@anisubbd'
CHANNEL_ID = -1003248434147

def upload_to_telegram(file_path, title, caption, progress_callback=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _upload():
            async with Client('bot', api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True) as app:
                def progress(current, total):
                    if progress_callback and total:
                        progress_callback(int(current/total*100))
                
                msg = await app.send_video(
                    chat_id=CHANNEL_USERNAME,
                    video=file_path,
                    caption=f'**{title}**\n{caption}' if caption else f'**{title}**',
                    supports_streaming=True,
                    progress=progress
                )
                return f'https://t.me/c/1003248434147/{msg.id}'
                
        return loop.run_until_complete(_upload())
    except Exception as e:
        raise Exception(f"Telegram upload failed: {e}")
    finally:
        loop.close()
