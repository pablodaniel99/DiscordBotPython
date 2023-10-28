
import asyncio
import base64
import discord
import logging
import starlette
import starlette.applications
import starlette.config
import starlette.status
import time
import types
import uvicorn
import websockets

"""

Basic:

- This bot consists of two main classes Web and Discord
- The Discord class handles the discord-users
- The Web class handles the web-users
- Both classes are strongly coupled and use the respective send method of each
  other E.g. if a web-user writes a message, the Web class calls the send method
  of the Discord class and vice versa.
- Just to clarify: The Web class itself does not directly send message from
  web-users to other web-users.

@startuml
WebUser1 -> Web: send message
Web -> Discord: call send_message
Discord -> Discord: on_message
Discord -> Web: send message to all web users
Web -> WebUser1: send message
WebUser1 -> WebUser1: display sent message
@enduml

http://www.plantuml.com/plantuml/png/SoWkIImgAStDuGfFJGejJYqoLD2rKm2ohHIAK_DI579JYuiJqrD1iY09bypYvFoY52k5vCIS7B2AU9WAg1IAglmyT6cifYkKv2k0p2i7Mb8AT4Cnp3gOcz0S0nD6LPAIMLoGarW9Kbe2L-e0r0Vq7G00

Ideas:

- Register: Currently author and guildid and channelid, are sent with every
  message. A web-user see Discord message only after it send its own message
  first. Is it better idea to add a "register" message and register the user
  when he opens the web page?
- Delivered: Bot may add an emoji to every message in Discord, if a message is
  delivered to all web-users (await message.add_reaction('✅'))
- 1:1 Chats: For every web-user, there is a "room" within Discord, just for this
  web-user.
- Spam: Detect spam from web-users.

Implementation:

- Basic Authetication: Is there a middleware available? Not just the example on
  starlette?
- Proper Shutdown: Currently starlette reacts to Ctrl+C and as consequence this
  application is shut down, without properly shutting down the discord
  connection
- Decoupling: At the moment the classes Gateway, Discord and Web are stroungly
  coupled. There is much room for improvment here.
- Connection Lookup: Currently all connections are stored in simple list. If the
  bot has to handle several guilds and many web connections it may be more
  efficient to use a another data structure to lookup web users.
- Connection Management: WebSockets are stored in an additional list. Maybe
  uvicorn or starlette already have such a list.
"""

config = starlette.config.Config('.env')

DISCORD_TOKEN = config('DISCORD_TOKEN')
WEB_STATUS_USERNAME = config('WEB_STATUS_USERNAME')
WEB_STATUS_PASSWORD = config('WEB_STATUS_PASSWORD')

DEMO_WEBSOCKET_SERVER = config('DEMO_WEBSOCKET_SERVER')
DEMO_DISCORD_GUILDID = config('DEMO_DISCORD_GUILDID')
DEMO_DISCORD_CHANNELID = config('DEMO_DISCORD_CHANNELID')

ENABLE_FAKE_DISCORD = config('ENABLE_FAKE_DISCORD', cast=bool, default=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('hermes')

def dict2obj(d):
	"""
	d = {'a': 2}
	d['a']  # -> 2
	d.a  # AttributeError: 'dict' object has no attribute 'a'
	o = dict2obj(d)
	o.a  # -> 2
	"""
	return types.SimpleNamespace(**d)

# Web

"""
https://www.starlette.io/
"""

class Web:
	def __init__(self, gateway):
		self.gateway = gateway

	def root(self, request):
		"""
		$ curl http://127.0.0.1:8004
		"""
		return starlette.responses.PlainTextResponse('Up and Alive\n')

	def status(self, request):
		"""
		$ curl --silent --user user:pw http://127.0.0.1:8004/status | jq
		"""
		RESPONSE_UNAUTHORIZED = starlette.responses.Response('', status_code=starlette.status.HTTP_401_UNAUTHORIZED, headers={'WWW-Authenticate': 'Basic realm=Status'})
		if 'Authorization' not in request.headers:
			return RESPONSE_UNAUTHORIZED
		try:
			authorization = request.headers['Authorization']
			scheme, credentials_encoded = authorization.split(' ', 2)
			assert scheme == 'Basic'
			credentials = base64.b64decode(credentials_encoded).decode('ascii')
			username, password = credentials.split(':', 2)
		except:
			return starlette.responses.Response('', status_code=starlette.status.HTTP_400_BAD_REQUEST)
		if (username != WEB_STATUS_USERNAME) or (password != WEB_STATUS_PASSWORD):
			return RESPONSE_UNAUTHORIZED

		runtime_seconds = int(time.time() - self.gateway.time_started)
		return starlette.responses.JSONResponse({
			'runtime_seconds': runtime_seconds,
			'web': self.info(),
			'discord': self.gateway.discord.info(),
		})

	def info(self):
		connected = []
		for connection in self.connections:
			host, port = connection.websocket.client.host, connection.websocket.client.port
			headers = connection.websocket.headers
			connected.append({
				'host': host,
				'port': port,
				'headers': { name:headers[name] for name in headers },
				'guilid': connection.guildid,
				'channelid': connection.channelid,
				'connectedMsg': connection.connectedMsg,
				'disconnectedMsg': connection.disconnectedMsg,
			})
		return {
			'connected': connected,
			'count_connections': self.count_connections,
		}

	def demo(self, request):
		html = open('demo.html', 'r').read()
		html = html.replace('DEMO_WEBSOCKET_SERVER', DEMO_WEBSOCKET_SERVER)
		html = html.replace('DEMO_DISCORD_GUILDID', DEMO_DISCORD_GUILDID)
		html = html.replace('DEMO_DISCORD_CHANNELID', DEMO_DISCORD_CHANNELID)
		return starlette.responses.HTMLResponse(html)

	async def handle_message(self, connection, message):
		"""
		$ wget https://github.com/vi/websocat/releases/download/v1.8.0/websocat_amd64-linux
		$ echo '{"type":"ping"}' | ./websocat_amd64-linux --one-message --no-close ws://127.0.0.1:8004/ws
		"""
		if message.type == 'ping':
			await connection.websocket.send_json({'type': 'pong'})
		elif message.type == 'text':
			# guildid is None if a web user has not sent any message yet
			if connection.guildid is None:
				connection.guildid, connection.channelid, connection.connectedMsg, connection.disconnectedMsg = int(message.guildid), int(message.channelid), message.connectedMsg, message.disconnectedMsg
				await self.gateway.discord.send_message(connection.guildid, connection.channelid, connection.connectedMsg)
			await self.gateway.discord.send_message(connection.guildid, connection.channelid, message.text)

	async def websocket(self, websocket):
		await websocket.accept()
		connection = dict2obj({
			'websocket': websocket,
			'guildid': None,
			'channelid': None,
			'connectedMsg': 'Connected',
			'disconnectedMsg': 'Disconnected',
		})
		self.connections.append(connection)
		self.count_connections += 1
		logger.info('Web: Connect')
		try:
			while True:
				message = dict2obj(await websocket.receive_json())
				await self.handle_message(connection, message)
		except starlette.websockets.WebSocketDisconnect:
			logger.info('Web: Disconnect')
		except websockets.exceptions.ConnectionClosedOK:
			logger.info('Web: Closed')
		except Exception as e:
			logger.error('Web: Error ' + repr(e))
		# all exceptions are catched, it is guaranted (and especially important for the connection) that the following commands are executed
		self.connections.remove(connection)
		await self.gateway.discord.send_message(connection.guildid, connection.channelid, connection.disconnectedMsg)

	async def start(self):
		self.count_connections = 0
		self.connections = []
		routes = [
			starlette.routing.Route('/', self.root),
			starlette.routing.Route('/demo', self.demo),
			starlette.routing.Route('/status', self.status),
			starlette.routing.WebSocketRoute('/ws', self.websocket),
		]
		app = starlette.applications.Starlette(debug=False, routes=routes)
		# assuming that this is executed in a container, and the port can be set in the container configuration
		config = uvicorn.Config(app=app, port=8004, host='0.0.0.0')
		server = uvicorn.Server(config)
		await server.serve()

# Discord

"""
https://discordpy.readthedocs.io/en/stable/#manuals
https://discordpy.readthedocs.io/en/latest/api.html
https://discord.com/developers/docs/topics/rate-limits
"""

class Discord(discord.Client):
	def __init__(self, gateway):
		super().__init__()
		self.gateway = gateway

	async def start(self):
		await self.login(DISCORD_TOKEN)
		await self.connect()

	async def on_ready(self):
		logger.info('Discord: Ready')

	async def on_message(self, message):
		for connection in self.gateway.web.connections:
			# only send message from discord-user to a web-user, if the web-user has registered itself to a channel
			if (message.guild.id == connection.guildid) and (message.channel.id == connection.channelid):
				await connection.websocket.send_json({
					'type': 'text',
					'author': message.author.display_name,  # author.display_name != author.name
					'channel': message.channel.name,
					'text': message.content,
				})

	async def send_message(self, guildid, channelid, text):
		guild = discord.utils.get(self.guilds, id=guildid)
		if guild is None:
			logger.debug('Discord: guild {0} not found'.format(guildid))
			return
		channel = discord.utils.get(guild.channels, id=channelid)
		if type(channel) != discord.channel.TextChannel:
			logger.debug('Discord: channel {0} not found'.format(channelid))
			return
		await channel.send(text)

	def info(self):
		return {
			'user': self.user.name,
		}

class FakeDiscord():
	"""
	Acts somewhat like the Discord class, without connecting to a Discord server
	"""

	def __init__(self, gateway):
		self.gateway = gateway

	async def start(self):
		asyncio.create_task(self._task())
		logger.info('FakeDiscord: Ready')

	async def send_message(self, guildid, channelid, text):
		await self._send_all(text)

	async def _send_all(self, text):
		for connection in self.gateway.web.connections:
			if (connection.guildid is not None) and (connection.channelid is not None):
				await connection.websocket.send_json({
					'type': 'text',
					'author': 'FakeAuthor',
					'channel': 'FakeChannel',
					'text': text,
				})

	async def _task(self):
		while True:
			text = 'Time ' + str(int(time.time()))
			logger.debug('FakeDiscord: ' + text)
			await self._send_all(text)
			await asyncio.sleep(5.0)

	def info(self):
		return {'user': 'FakeUser'}

# Gateway

class Gateway:
	def __init__(self):
		self.time_started = time.time()
		self.web = Web(self)
		self.discord = FakeDiscord(self) if ENABLE_FAKE_DISCORD else Discord(self)

	def start(self):
		task_web = self.web.start()
		task_discord = self.discord.start()

		loop = asyncio.get_event_loop()
		loop.create_task(task_discord)
		# Intentionally only wait until starlette is not running anymore, because at the moment only starlette handles Ctrl+C properly
		loop.run_until_complete(task_web)

def start():
	gateway = Gateway()
	gateway.start()

start()
