# SonosCast

This software is VERY alpha-quality. Expect things not working and audio stuttering.

How to use:

- Edit `server.py`, and change MY_MAC to your MAC, MY_IP to your IP.
- run `make` to compile the native parts
- run `pip3 install aiohttp aiohttp_jinja2`
- Run `python3 server.py`
- You'll see a new Sonos device appear with the name you set above. Play its Line-In in any of your other devices!
