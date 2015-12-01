#!/usr/bin/python
#
# This file is part of Bargate.
#
# Bargate is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Bargate is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Bargate.  If not, see <http://www.gnu.org/licenses/>.

from bargate import app
import bargate.core
from flask import Flask, request, session, redirect, url_for, flash, g, abort, make_response
import kerberos
import mimetypes
import os
import time
import json
import re
import werkzeug
from itsdangerous import base64_decode
import ldap

################################################################################
#### HOME PAGE / LOGIN PAGE

@app.route('/', methods=['GET', 'POST'])
@bargate.core.downtime_check
def login():
	if bargate.core.is_user_logged_in():
		return redirect(url_for(app.config['SHARES_DEFAULT']))
	else:
		if request.method == 'GET' or request.method == 'HEAD':
			next = request.args.get('next',default=None)
			return bargate.core.render_page('login.html', next=next)

		elif request.method == 'POST':

			result = bargate.core.auth_user(request.form['username'], request.form['password'])

			if not result:
				flash('Incorrect username and/or password','alert-danger')
				return redirect(url_for('login'))

			## Set the username in the session
			session['username']  = request.form['username'].lower()

			## Check if the user selected "Log me out when I close the browser"
			if app.config['REMEMBER_ME_ENABLED']:
				permanent = request.form.get('sec',default="")

				## Set session as permanent or not
				if permanent == 'sec':
					session.permanent = True
				else:
					session.permanent = False
			else:
				session.permanent = False

			## Encrypt the password and store in the session
			session['id'] = bargate.core.aes_encrypt(request.form['password'],app.config['ENCRYPT_KEY'])

			## Check if two-factor is enabled for this account
			## TWO STEP LOGONS
			if app.config['TOTP_ENABLED']:
				if bargate.totp.totp_user_enabled(session['username']):
					app.logger.debug('User "' + session['username'] + '" has two step enabled. Redirecting to two-step handler')
					return redirect(url_for('totp_logon_view',next=request.form.get('next',default=None)))

			## Successful logon without 2-step needed
			return bargate.core.logon_ok()


################################################################################
#### LOGOUT

@app.route('/logout')
@bargate.core.login_required
def logout():
	## Record the logout
	bargate.settings.set_user_data('logout',str(time.time()))

	## Log out of the session
	bargate.core.session_logout()

	## Tell the user
	flash('You were logged out successfully','alert-success')

	## redirect the user to the logon page
	return redirect(url_for('login'))

################################################################################
#### HELP PAGES

@app.route('/about')
def about():
	return bargate.core.render_page('about.html', active='help')

@app.route('/about/changelog')
def changelog():
	return bargate.core.render_page('changelog.html', active='help')

@app.route('/nojs')
def nojs():
	return bargate.core.render_page('nojs.html')

@app.route('/test')
def test():
	return bargate.core.render_page('test.html')

################################################################################
#### MIME MAP (DEVELOPER FUNCTION)

@app.route('/mime')
@bargate.core.downtime_check
@bargate.core.login_required
def mime():
	mimetypes.init()
	return bargate.core.render_page("mime.html",types=mimetypes.types_map,active="help")

################################################################################
#### BOOKMARKS

@app.route('/bookmarks', methods=['GET','POST'])
@bargate.core.login_required
@bargate.core.downtime_check
def bookmarks():
	bmKey = 'user:' + session['username'] + ':bookmarks'
	bmPrefix = 'user:' + session['username'] + ':bookmark:'

	if request.method == 'GET':
		bookmarks = bargate.settings.get_user_bookmarks()
		return bargate.core.render_page('bookmarks.html', active='user',pwd='',bookmarks = bookmarks)

	elif request.method == 'POST':
		action = request.form['action']

		if action == 'add':

			try:
				bookmark_name     = request.form['bookmark_name']
				bookmark_function = bargate.smb.check_name(request.form['bookmark_function'])
				bookmark_path     = bargate.smb.check_path(request.form['bookmark_path'])

			except KeyError as e:
				bargate.errors.fatal('Invalid Bookmark','You missed something on the previous page!')
			except ValueError as e:
				bargate.errors.fatal('Invalid Bookmark','Invalid bookmark name or bookmark value: ' + str(e))

			try:
				test_name = url_for(str(bookmark_function),path=bookmark_path)
			except werkzeug.routing.BuildError as ex:
				bargate.errors.fatal('Invalid Bookmark','Invalid function and/or path: ' + str(ex))

			g.redis.hset(bmPrefix + bookmark_name,'function',bookmark_function)
			g.redis.hset(bmPrefix + bookmark_name,'path',bookmark_path)
			g.redis.sadd(bmKey,bookmark_name)

			flash('Bookmark added successfully','alert-success')
			## return the user to the folder they were in
			return redirect(url_for(bookmark_function,path=bookmark_path))

		elif action == 'delete':
			bookmark_name     = request.form['bookmark_name']

			if g.redis.exists(bmKey):
				if g.redis.sismember(bmKey,bookmark_name):

					## Delete from the bookmarks key
					g.redis.srem(bmKey,bookmark_name)

					## Delete the bookmark hash
					g.redis.delete(bmPrefix + bookmark_name)

					## Let the user know and redirect
					flash('Bookmark deleted successfully','alert-success')
					return redirect(url_for('bookmarks'))

			flash('Bookmark not found!','alert-danger')
			return redirect(url_for('bookmarks'))

################################################################################
#### Who is online?

@app.route('/online/<last>')
@bargate.core.login_required
@bargate.core.downtime_check
def online(last=5):
	last = int(last)

	if last == 1440:
		last_str = "24 hours"
	elif last == 60:
		last_str = "hour"
	elif last == 120:
		last_str = "2 hours"
	elif last == 180:
		last_str = "3 hours"
	else:
		last_str = str(last) + " minutes"

	usernames = bargate.core.list_online_users(last)
	return 	bargate.core.render_page('online.html',online=usernames,active="help",last=last_str)

#################################################################################
### Portal login support (added for University of Sheffield)

@app.route('/portallogin', methods=['POST', 'GET'])
def portallogin():
	if request.method == 'GET':
		cookie_name    = request.args.get('cookie0')
		cookie_content = request.args.get('cookie1').split(';')[0]
	elif request.method == 'POST':
		cookie_name    = request.form.getlist('cookie0')
		cookie_content = request.form.getlist('cookie1').split(';')[0]

	decoded_cookie_content = bargate.core.decode_session_cookie(cookie_content)
	json_cookie_content    = bargate.core.flask_load_session_json(decoded_cookie_content)

	app.logger.info('Decoded cookie username ' + json_cookie_content['username'])

	session['username']     = json_cookie_content['username']
	session['id']           = json_cookie_content['id']

	## verify this username and password we've been told to accept via cookie
	result = bargate.core.auth_user(session['username'], bargate.core.get_user_password())

	if not result:
		flash('Incorrect username and/or password','alert-danger')
		bargate.core.session_logout()
		return redirect(url_for('login'))
	else:
		session['logged_in']    = True
		return redirect(url_for(app.config['SHARES_DEFAULT']))
