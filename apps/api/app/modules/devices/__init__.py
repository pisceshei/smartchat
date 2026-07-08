"""WhatsApp-App / LINE-App device-bridge provisioning (QR-scan flow).

The personal-account channels pair by scanning a QR code with the phone, not by
pasting a token, so they do NOT use the generic connect form. ``service`` owns
the provisioning lifecycle (create account + device_bridge, drive the whatsmeow
bridge, poll health, logout, teardown); ``router`` exposes the QR/status/logout
endpoints. The create step is dispatched from channels/router.py's
connect_account (which shares the ``/channels/{channel_type}/accounts`` path).
"""
