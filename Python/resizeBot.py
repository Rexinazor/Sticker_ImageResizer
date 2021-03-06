import os
import sys
import time
import io
import signal
import logging

from hashlib import sha1
from inspect import cleandoc
from datetime import datetime

import coloredlogs
import cursor
import redis
import telegram

from PIL import Image, UnidentifiedImageError
from resizeimage import resizeimage
from telegram import InputFile, File
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

from utils import load_config, time_delta_to_legible_eta

# keep track of time spent running
STARTUP_TIME = time.time()

# connect to redis for stats
rd = redis.Redis(host='localhost', port=6379, db=3, decode_responses=True)

def start(update, context):
	'''
	Responds to /start and /help commands.
	'''
	# construct message
	reply_msg = f'''🖼 *Resize Image for Stickers v{VERSION}*

	To resize an image to a sticker-ready format, just send it to this chat!

	[Managed By BioHazard Network.](https://t.me/TheHazardNetwork)
	'''

	# pull chat id, send message
	chat_id = update.message.chat.id
	context.bot.send_message(chat_id, cleandoc(reply_msg), parse_mode='Markdown')

	logging.info(f'🌟 Bot added to a new chat! chat_id={chat_id}.')


def helpc(update, context):
	'''
	Responds to /help command
	'''
	# construct message
	reply_msg = '''🖼 To use the bot, simply send your image to this chat (jpg/png/webp).

	Hint: you can also send multiple images at once!
	'''

	# pull chat id, send message
	chat_id = update.message.chat.id
	context.bot.send_message(chat_id, cleandoc(reply_msg), parse_mode='Markdown')

	logging.info(f'🤖 Chat {chat_id} requested help.')


def source(update, context):
	'''
	Responds to /source command
	'''
	# construct message
	reply_msg = '''🐙 [Source on Github.](https://github.com/Rexinazor/Sticker_ImageResizer)
	'''

	# pull chat id, send message
	chat_id = update.message.chat.id
	context.bot.send_message(chat_id, cleandoc(reply_msg), parse_mode='Markdown')

	logging.info(f'🐙 Chat {chat_id} requested Github link!')


def statistics(update, context):
	'''
	Responds to /stats command
	'''
	if rd.exists('converted-imgs'):
		imgs = int(rd.get('converted-imgs'))
	else:
		imgs = 0

	if rd.exists('chats'):
		chats = rd.get('chats')
		chats = len(chats.split(','))
	else:
		chats = 0

	sec_running = int(time.time()) - STARTUP_TIME
	runtime = time_delta_to_legible_eta(time_delta=sec_running, full_accuracy=False)

	msg = f'''📊 *Bot statistics*
	Images converted: {imgs:,}
	Unique chats seen: {chats:,}
	Bot started {runtime} ago
	'''

	context.bot.send_message(update.message.chat.id, cleandoc(msg), parse_mode='Markdown')


def document_to_bytearray(update, context):
	'''
	Handle uncompressed images sent to the bot
	'''
	# load file, download as byte array
	try:
		file = update.message.document.get_file()
	except telegram.error.BadRequest as e:
		msg = f'⚠️ Telegram was unable to download your file: please try again. Cause: {e}'
		context.bot.send_message(update.message.chat.id, cleandoc(msg), parse_mode='Markdown')
		return
	except telegram.error.TimedOut:
		try:
			file = update.message.document.get_file()
		except Exception as e:
			msg = f'⚠️ Telegram was unable to download your file: please try again later. Cause: {e}'
			context.bot.send_message(update.message.chat.id, cleandoc(msg), parse_mode='Markdown')
			return

	img_bytes = file.download_as_bytearray()

	convert_img(
		update=update, context=context,
		img_bytes=img_bytes, ftype='File')


def photo_to_bytearray(update, context):
	'''
	Handle compressed images sent to the bot
	'''
	# load img
	photo = update.message.photo[-1]

	try:
		photo_file = photo.get_file()
	except telegram.error.BadRequest as e:
		msg = f'⚠️ Telegram was unable to download your photo: please try again. Cause: {e}'
		context.bot.send_message(update.message.chat.id, cleandoc(msg), parse_mode='Markdown')
		return
	except telegram.error.TimedOut as e:
		try:
			photo_file = photo.get_file()
		except Exception as e:
			msg = f'⚠️ Telegram was unable to download your photo: please try again. Cause: {e}'
			context.bot.send_message(update.message.chat.id, cleandoc(msg), parse_mode='Markdown')
			return

	img_bytes = photo_file.download_as_bytearray()

	# send byte array to convert_imt
	convert_img(
		update=update, context=context,
		img_bytes=img_bytes, ftype='Photo')


def convert_img(update, context, img_bytes, ftype):
	'''
	Converts the image to the desired format, e.g. to a png-formatted image
	with a 512 pixel wide longest side.
	'''
	# log start
	logging.info(f'🖼 [{update.message.chat.id}] {ftype} loaded: starting image conversion...')

	# load image
	try:
		img = Image.open(io.BytesIO(img_bytes))
	except UnidentifiedImageError:
		logging.error(f'\t[{update.message.chat.id}] Unknown image type: notifying user.')

		context.bot.send_message(
			text='⚠️ Error: file is not a jpg/png/webp',
			chat_id=update.message.chat.id)

	if img.format in ('JPEG', 'WEBP'):
		img = img.convert('RGB')
	elif img.format == 'PNG':
		pass
	else:
		logging.info(f'\t[{update.message.chat.id}] Image conversion failed: not a jpg/png/webp!')
		context.bot.send_message(
			text='⚠️ Error: file is not a jpg/png/webp',
			chat_id=update.message.chat.id)
		return

	# read image dimensions
	w, h = img.size

	# resize larger side to 512
	upscaled = False
	if w >= h:
		try:
			img = resizeimage.resize_width(img, 512)
		except Exception as error:
			# error: image width is probably smaller than 512 px
			# do a linear resize
			w_res_factor = 512/w
			img = img.resize((512, int(h*w_res_factor)), resample=Image.NEAREST)
			upscaled = True
	else:
		try:
			img = resizeimage.resize_height(img, 512)
		except Exception as error:
			# error: image height is probably smaller than 512 px
			# do a linear resize
			h_res_factor = 512/h
			img = img.resize((int(w*h_res_factor), 512), resample=Image.NEAREST)
			upscaled = True

	if upscaled:
		logging.info(f'\tImage upscaled due to small size ({w}x{h}): user will be warned')

	# read width, height of new image
	w, h = img.size

	# save image to buffer
	byte_arr = io.BytesIO()
	img.save(byte_arr, format='PNG', compress_level=0)

	# compress if size > 512 KB (kibi, not kilo)
	compression_failed = False
	if byte_arr.tell() / 1024 > 512:
		fsize = byte_arr.tell() / 1024
		compression_level, optimize = 1, False

		logging.warning(f'\tImage is too large ({fsize:.2f} KB): compressing...')
		while fsize > 512:
			if compression_level > 9:
				optimize, compression_level = True, 9

			temp = io.BytesIO()
			img.save(
				temp, format='PNG', optimize=optimize,
				compression_level=compression_level)

			fsize = temp.tell() / 1024
			byte_arr = temp

			logging.warning(f'\t\t{fsize:.2f} KB | clevel={compression_level}, optimize={optimize}')
			compression_level += 1
			if optimize:
				if fsize >= 512:
					compression_failed = True
				break

	# generate a random filename
	random_hash = sha1(
		str(time.time()).encode('utf-8')).hexdigest()[0:6]
	random_filename = f'image-{random_hash}.png'

	# create telegram.InputFile object by reading raw bytes
	byte_arr.seek(0)
	img_file = InputFile(byte_arr, filename=random_filename)

	image_caption = f"🖼 Here's your sticker-ready image ({w}x{h})! Forward this to @Stickers."
	if compression_failed:
		image_caption += '\n\n⚠️ Image compression failed (≥512 KB): '
		image_caption += 'you must manually compress the image!'
	elif upscaled:
		image_caption += '\n\n⚠️ Image upscaled! Quality may have been lost: '
		image_caption += 'consider using a larger image.'

	sent = False
	while not sent:
		try:
			context.bot.send_document(
				chat_id=update.message.chat.id, document=img_file,
				caption=image_caption,
				filename=f'resized-image-{int(time.time())}.png'
			)

			sent = True
		except telegram.error.TimedOut:
			logging.warning('\tError sending: timed out... Trying again in 2 seconds.')
			time.sleep(2)
		except Exception as error:
			logging.exception(f'Error sending document: {error}.')
			return

	# add +1 to stats
	if rd.exists('converted-imgs'):
		rd.set('converted-imgs', int(rd.get('converted-imgs')) + 1)
	else:
		rd.set('converted-imgs', 1)

	if rd.exists('chats'):
		chat_list = rd.get('chats').split(',')
		if str(update.message.chat.id) not in chat_list:
			chat_list.append(str(update.message.chat.id))
			rd.set('chats', ','.join(chat_list))
	else:
		rd.set('chats', str(update.message.chat.id))

	logging.info(f'\t[{update.message.chat.id}] Successfully converted image!')


def sigterm_handler(signal, frame):
	'''
	Logs program run time when we get sigterm.
	'''
	logging.info(f'✅ Got SIGTERM. Runtime: {datetime.now() - STARTUP_TIME}.')
	logging.info(f'Signal: {signal}, frame: {frame}.')
	sys.exit(0)


if __name__ == '__main__':
	VERSION = '1.3.3'
	DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
	DEBUG = True

	# load config, load bot
	config = load_config(data_dir=DATA_DIR)
	updater = Updater(config['bot_token'], use_context=True)

	# init log (disk)
	log = os.path.join(DATA_DIR, 'log-file.log')
	logging.basicConfig(
		filename=log, level=logging.DEBUG, format='%(asctime)s %(message)s', datefmt='%d/%m/%Y %H:%M:%S')

	# disable logging for urllib and requests because jesus fuck they make a lot of spam
	logging.getLogger('urllib3').setLevel(logging.CRITICAL)
	logging.getLogger('chardet.charsetprober').setLevel(logging.CRITICAL)
	logging.getLogger('telegram').setLevel(logging.ERROR)
	logging.getLogger('telegram.bot').setLevel(logging.ERROR)
	logging.getLogger('telegram.ext.updater').setLevel(logging.ERROR)
	logging.getLogger('telegram.vendor').setLevel(logging.ERROR)
	logging.getLogger('PIL').setLevel(logging.ERROR)
	logging.getLogger('telegram.error.TelegramError').setLevel(logging.ERROR)
	logging.getLogger('telegram.error.NetworkError').setLevel(logging.ERROR)
	coloredlogs.install(level='DEBUG')

	# get the dispatcher to register handlers
	dispatcher = updater.dispatcher

	# handle pre-compressed photos
	dispatcher.add_handler(
		MessageHandler(
			Filters.photo, callback=photo_to_bytearray))

	# handle files in a separate function
	dispatcher.add_handler(
		MessageHandler(
			Filters.document.category("image") & ~Filters.photo,
			callback=document_to_bytearray))

	# handle commands
	dispatcher.add_handler(CommandHandler(command=('start'), callback=start))
	dispatcher.add_handler(CommandHandler(command=('source'), callback=source))
	dispatcher.add_handler(CommandHandler(command=('help'), callback=helpc))
	dispatcher.add_handler(CommandHandler(command=('stats'), callback=statistics))

	# all up to date, start polling
	updater.start_polling()

	# handle sigterm
	signal.signal(signal.SIGTERM, sigterm_handler)

	# hide cursor for pretty print
	try:
		if not DEBUG:
			cursor.hide()
			while True:
				for char in ('⠷', '⠯', '⠟', '⠻', '⠽', '⠾'):
					sys.stdout.write('%s\r' % '  Connected to Telegram! To quit: ctrl + c.')
					sys.stdout.write('\033[92m%s\r\033[0m' % char)
					sys.stdout.flush()
					time.sleep(0.1)
		else:
			while True:
				time.sleep(10)
	except KeyboardInterrupt:
		# on exit, show cursor as otherwise it'll stay hidden
		cursor.show()
		logging.info('Ending...')
		updater.stop()
