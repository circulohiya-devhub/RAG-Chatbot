"""
All chat channels the server knows about.
To add a new channel: create channels/<name>.py with a `channel` and a `router`
(copy facebook.py as a template), then add it to ALL_CHANNELS below.
A channel is only switched on when its settings are present in .env.
"""
from . import facebook, viber, web, whatsapp

ALL_CHANNELS = [facebook, whatsapp, viber, web]
