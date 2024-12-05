# !/usr/bin/env python
# encoding: utf-8
#
# This file is part of ckanext-contact
# Created by the Natural History Museum in London, UK
import logging
import socket
from ckan import logic
from ckan.common import asbool
from ckan.lib import mailer
from ckan.lib.navl.dictization_functions import unflatten
from ckan.plugins import PluginImplementations, toolkit
from ckanext.contact import recaptcha
from ckanext.contact.interfaces import IContact
from datetime import datetime, timezone

from flask import render_template

from markupsafe import escape


log = logging.getLogger(__name__)


def validate(data_dict):
    '''
    Validates the given data and recaptcha if necessary.

    :param data_dict: the request params as a dict
    :return: a 3-tuple of errors, error summaries and a recaptcha error, in the event where no
             issues occur the return is ({}, {}, None)
    '''
    errors = {}
    error_summary = {}
    recaptcha_error = None

    # check the three fields we know about
    for field in ('email', 'name', 'content'):
        value = data_dict.get(field, None)
        if value is None or value == '':
            errors[field] = ['Missing Value']
            error_summary[field] = 'Missing value'

    # only check the recaptcha if there are no errors
    if not errors:
        try:
            expected_action = toolkit.config.get('ckanext.contact.recaptcha_v3_action')
            # check the recaptcha value, this only does anything if recaptcha is setup
            recaptcha.check_recaptcha(data_dict.get('g-recaptcha-response', None), expected_action)
        except recaptcha.RecaptchaError as e:
            log.info(f'Recaptcha failed due to "{e}" : {expected_action}')
            recaptcha_error = toolkit._('Recaptcha check failed, please try again.')

    return errors, error_summary, recaptcha_error


def build_subject(form_variant='contact', contact_type='Question', subject_default='Contact from visitor', timestamp_default=False):
    '''
    Creates the subject line for the contact email using the config or the defaults.

    :param subject_default: the default str to use if ckanext.contact.subject isn't specified
    :param timestamp_default: the default bool to use if add_timestamp_to_subject isn't specified
    :return: the subject line
    '''
    subject = toolkit.config.get(f'ckanext.{form_variant}.subject', toolkit._(subject_default))
    if( form_variant == 'contact' ):
        subject = '{} : {}'.format( subject, contact_type )
    if asbool(toolkit.config.get('ckanext.contact.add_timestamp_to_subject', timestamp_default)):
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')
        subject = f'{subject} [{timestamp}]'
    return subject


def submit():
    '''
    Take the data in the request params and send an email using them. If the data is invalid or
    a recaptcha is setup and it fails, don't send the email.

    :return: a dict of details
    '''
    # this variable holds the status of sending the email
    email_success = True

    # pull out the data from the request
    data_dict = logic.clean_dict(
        unflatten(logic.tuplize_dict(logic.parse_params(toolkit.request.values)))
    )

    # validate the request params
    errors, error_summary, recaptcha_error = validate(data_dict)

    # if there are not errors and no recaptcha error, attempt to send the email
    if len(errors) == 0 and recaptcha_error is None:

        # set default form variant if not set
        if( data_dict['form_variant'] == '' ):
            data_dict['form_variant'] = 'contact'

        body_parts = [ f'{data_dict["content"]}\n' ];
        body_parts.append( 'Sent by:' )
        body_parts.append( f'  Name: {data_dict["name"]}' )
        body_parts.append( f'  Email: {data_dict["email"]}' )
        # Add the dataset URL if there is one
        if( "pkg-url" in data_dict and data_dict["pkg-url"] != "" ):
            body_parts.append( f'  Dataset URL: {data_dict["pkg-url"]}' )
        else:
            data_dict["pkg-url"] = ""
        
        if( "contact_type" not in data_dict or data_dict["contact_type"] == "" ):
            data_dict["contact_type"] = "Question"
            body_parts.append( f'  Contact Type: {data_dict["contact_type"]}' )

        if( data_dict["form_variant"] == 'suggest_dataset' ):
            # add 'suggest dataset' fields to email body
            if( "resource" not in data_dict or data_dict["resource"] == "" ):
                data_dict["resource"] = "N/A"
            if( "maintainer" not in data_dict or data_dict["maintainer"] == "" ):
                data_dict["maintainer"] = "N/A"
            if( "url" not in data_dict or data_dict["url"] == "" ):
                data_dict["url"] = "N/A"
            if( data_dict["contact_type"] == 'Both' ):
                # set this for readability in email, otherwise 'Both' is out of context
                data_dict["contact_type"] = "Data and Application"

            body_parts.append( f'  Title of Resource: {data_dict["resource"]}' )
            body_parts.append( f'  Who owns or maintains this resource? {data_dict["maintainer"]}' )
            body_parts.append( f'  Link: {data_dict["url"]}' )



        else:
            # set 'suggest data' fields to empty so render_template won't break for regular contact message
            data_dict['resource'] = '';
            data_dict['maintainer'] = '';
            data_dict['url'] = '';

        mail_dict = {
            'recipient_email': toolkit.config.get('ckanext.contact.mail_to',
                                                  toolkit.config.get('email_to')),
            'recipient_name': toolkit.config.get('ckanext.contact.recipient_name',
                                                 toolkit.config.get('ckan.site_title')),
            'subject': build_subject( data_dict["form_variant"], data_dict['contact_type'] ),
            'body': '\n'.join(body_parts),

            'body_html': render_template(
                f'emails/{data_dict["form_variant"]}.html',
                name = data_dict['name'],
                email = data_dict['email'],
                contact_type = data_dict['contact_type'],
                resource = data_dict['resource'],
                maintainer = data_dict['maintainer'],
                url = data_dict['url'],
                pkg_url = data_dict['pkg-url'],
                # pre-escape message so that we can add </br> tags safely in the Jinja2 template
                message = escape( data_dict['content'] ),
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z'),
                site_title = toolkit.config.get('ckan.site_title'), 
                site_url = toolkit.url_for( 'home.index', _external=True ),
                subject = build_subject( data_dict["form_variant"], data_dict['contact_type'] )
            ),

            # set reply-to to send to person submitting the form
            "headers": {
                "Reply-to": data_dict["email"]
            }
        }
        
        if( "contact_dest" not in data_dict ):
            data_dict["contact_dest"] = "data-hub-support"

        if( data_dict["contact_dest"] != "data-hub-support" and "pkg-id" in data_dict and data_dict["pkg-id"] != '' ):
            pkg = toolkit.get_action('package_show')(None, {'id': data_dict["pkg-id"] } )
            if( pkg["data_contact_email"] ): 
                mail_dict["headers"]["cc"] =  mail_dict["recipient_email"] 
                mail_dict["recipient_email"] = pkg["data_contact_email"]

        # allow other plugins to modify the mail_dict
        for plugin in PluginImplementations(IContact):
            plugin.mail_alter(mail_dict, data_dict)

        try:
            mailer.mail_recipient(**mail_dict)
        except (mailer.MailerException, socket.error):
            email_success = False

    return {
        'success': recaptcha_error is None and len(errors) == 0 and email_success,
        'data': data_dict,
        'errors': errors,
        'error_summary': error_summary,
        'recaptcha_error': recaptcha_error,
    }
