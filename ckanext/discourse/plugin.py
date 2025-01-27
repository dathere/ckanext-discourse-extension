import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
import ckan.logic as logic
import requests
import time
import re
import json
from datetime import timedelta
from ckan.plugins.toolkit import asbool
from ckan.common import config
from ckanext.discourse.interfaces import IDiscourse
from ckanext.discourse.discourse_api import DiscourseApi
import ckan.lib.jobs as jobs

import logging

log = logging.getLogger(__name__)

get_action = logic.get_action

REQUEST_TIMEOUT = 5

class DiscoursePlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurable)
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.ITemplateHelpers)
    plugins.implements(plugins.IPackageController, inherit=True)

    # Class-level attributes
    next_sync = 0
    topic_lookup_dict = {}
    active_conversations = 0
    discourse_category_id = ''
    discourse_metadata_fields = []
    discourse_url = None
    discourse_username = None
    discourse_count_cache_age = 60
    discourse_ckan_category = None
    discourse_debug = False
    discourse_api_key = None
    verify_ssl = True

    def configure(self, config):
        log.debug("Configuring Discourse plugin")

        # Load configuration values
        DiscoursePlugin.discourse_url = config.get('discourse.url')
        DiscoursePlugin.discourse_username = config.get('discourse.username')
        DiscoursePlugin.discourse_count_cache_age = int(config.get('discourse.count_cache_age', 60))
        DiscoursePlugin.discourse_ckan_category = config.get('discourse.ckan_category')
        DiscoursePlugin.discourse_debug = asbool(config.get('discourse.debug', False))
        DiscoursePlugin.discourse_api_key = config.get('discourse.api_key', '')
        DiscoursePlugin.discourse_category_id = config.get('discourse.category_id', '')
        DiscoursePlugin.discourse_metadata_fields = config.get('discourse.metadata_fields', '').split()
        DiscoursePlugin.verify_ssl = asbool(config.get('discourse.verify_ssl', True))

        # Validate required configuration
        if not DiscoursePlugin.discourse_url:
            log.error("Missing required configuration: discourse.url")
        if not DiscoursePlugin.discourse_ckan_category:
            log.error("Missing required configuration: discourse.ckan_category")

        # Initialize API client
        self.discourse_api = DiscourseApi(
            DiscoursePlugin.discourse_url,
            DiscoursePlugin.discourse_username,
            DiscoursePlugin.discourse_api_key,
            DiscoursePlugin.verify_ssl
        )

        # Initial sync
        if DiscoursePlugin.next_sync == 0:
            DiscoursePlugin.next_sync = time.time() + DiscoursePlugin.discourse_count_cache_age
            self.discourse_sync()

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
        toolkit.add_public_directory(config_, 'public')
        toolkit.add_resource('assets', 'discourse')

    @classmethod
    def discourse_sync(cls):
        if time.time() < cls.next_sync and cls.topic_lookup_dict:
            return cls.active_conversations

        log.info("Starting Discourse sync...")
        new_lookup = {}
        active = 0

        try:
            topics = cls.discourse_api.get_category_topics(cls.discourse_category_id)
            for topic in topics:
                # Get first post content to find CKAN dataset ID
                posts = cls.discourse_api.get_topic_posts(topic['id'])
                if posts:
                    first_post = posts[0]['raw']
                    match = re.search(r'<!-- CKAN_DATASET_ID: (.+) -->', first_post)
                    if match:
                        dataset_id = match.group(1)
                        comment_count = topic['posts_count'] - 1  # Subtract initial post
                        new_lookup[dataset_id] = comment_count
                        if comment_count > 0:
                            active += 1

            cls.topic_lookup_dict = new_lookup
            cls.active_conversations = active
            cls.next_sync = time.time() + cls.discourse_count_cache_age
            log.info(f"Sync complete. Found {active} active conversations.")

        except Exception as e:
            log.error(f"Sync failed: {str(e)}")
            cls.next_sync = time.time() + 60  # Retry sooner on failure

        return active

    @classmethod
    def discourse_comments(cls, canonical_url=''):
        context = {'ignore_auth': True}
        data = {
            'discourse_url': cls.discourse_url,
            'topic_id': '',
            'discourse_username': cls.discourse_username,
            'embed_url': canonical_url
        }

        try:
            if not canonical_url:
                pkg = toolkit.g.pkg_dict
                if pkg:
                    data['topic_id'] = pkg['name']
                    data['embed_url'] = toolkit.url_for('dataset.read', id=pkg['name'], _external=True)
            else:
                # Extract dataset name from URL
                match = re.search(r'/dataset/([^/]+)', canonical_url)
                if match:
                    data['topic_id'] = match.group(1)

            # Allow other plugins to modify the data
            for plugin in plugins.PluginImplementations(IDiscourse):
                data = plugin.before_render_comments(data)

        except Exception as e:
            log.error(f"Error preparing comments: {str(e)}")

        return toolkit.render_snippet(
            'discourse_comments_debug.html' if cls.discourse_debug else 'discourse_comments.html',
            data
        )

    @classmethod
    def discourse_comments_count(cls, topic_id):
        return cls.topic_lookup_dict.get(topic_id, 0)

    def get_helpers(self):
        return {
            'discourse_comments': self.discourse_comments,
            'discourse_comments_count': self.discourse_comments_count,
            'discourse_sync': self.discourse_sync,
            'discourse_category_url': lambda: f"{self.discourse_url}c/{self.discourse_category_id}",
            'discourse_sync_status': lambda: {
                'next_sync': self.next_sync,
                'active_conversations': self.active_conversations
            }
        }

    # IPackageController implementation
    def after_dataset_create(self, context, pkg_dict):
        if not pkg_dict.get('private'):
            jobs.enqueue(
                create_discourse_topic,
                args=[pkg_dict, self.discourse_category_id, self.discourse_metadata_fields,
                      self.discourse_api_key, self.discourse_username,
                      self.discourse_url, config.get('ckan.site_url')]
            )

    def after_dataset_update(self, context, pkg_dict):
        if not pkg_dict.get('private'):
            jobs.enqueue(
                update_discourse_topic,
                args=[pkg_dict, self.discourse_category_id, self.discourse_metadata_fields,
                      self.discourse_api_key, self.discourse_username,
                      self.discourse_url, config.get('ckan.site_url')]
            )

def create_discourse_topic(pkg_dict, category_id, metadata_fields, api_key, username, base_url, site_url):
    api = DiscourseApi(base_url, username, api_key)
    
    try:
        raw = _generate_discourse_content(pkg_dict, metadata_fields, site_url)
        response = api.create_topic(
            title=pkg_dict['title'],
            raw=raw,
            category_id=category_id,
            tags=pkg_dict.get('tags', [])
        )
        
        if response and 'id' in response:
            log.info(f"Created Discourse topic {response['id']} for dataset {pkg_dict['id']}")
        else:
            log.error("Failed to create Discourse topic")

    except Exception as e:
        log.error(f"Topic creation failed: {str(e)}")

def update_discourse_topic(pkg_dict, category_id, metadata_fields, api_key, username, base_url, site_url):
    api = DiscourseApi(base_url, username, api_key)
    
    try:
        # Find topic by embedded dataset ID
        topics = api.get_category_topics(category_id)
        for topic in topics:
            posts = api.get_topic_posts(topic['id'])
            if posts and f"<!-- CKAN_DATASET_ID: {pkg_dict['name']} -->" in posts[0]['raw']:
                raw = _generate_discourse_content(pkg_dict, metadata_fields, site_url)
                api.update_post(
                    post_id=posts[0]['id'],
                    raw=raw
                )
                log.info(f"Updated Discourse topic {topic['id']} for dataset {pkg_dict['name']}")
                return

        log.warning(f"No Discourse topic found for dataset {pkg_dict['name']}")

    except Exception as e:
        log.error(f"Topic update failed: {str(e)}")

def _generate_discourse_content(pkg_dict, metadata_fields, site_url):
    dataset_url = f"{site_url}/dataset/{pkg_dict['name']}"
    content = f"<!-- CKAN_DATASET_ID: {pkg_dict['name']} -->\n"
    content += f"<h1>{pkg_dict.get('title', pkg_dict['name'])}</h1>"
    
    if pkg_dict.get('notes'):
        content += f"<div class='ckan-description'>{pkg_dict['notes']}</div>"
    
    content += "<div class='ckan-metadata'>"
    for field in metadata_fields:
        if field in pkg_dict:
            content += f"<p><strong>{field.title()}</strong>: {pkg_dict[field]}</p>"
    
    content += f"<p><a href='{dataset_url}'>View on CKAN</a></p>"
    content += "</div>"
    
    return content

class DiscourseApi:
    def __init__(self, base_url, username, api_key, verify_ssl=True):
        self.base_url = base_url.rstrip('/')
        self.headers = {
            'Api-Key': api_key,
            'Api-Username': username,
            'Content-Type': 'application/json'
        }
        self.verify_ssl = verify_ssl

    def get_category_topics(self, category_id):
        try:
            response = requests.get(
                f"{self.base_url}/c/{category_id}.json",
                headers=self.headers,
                verify=self.verify_ssl
            )
            return response.json()['topic_list']['topics']
        except Exception as e:
            log.error(f"Error getting category topics: {str(e)}")
            return []

    def get_topic_posts(self, topic_id):
        try:
            response = requests.get(
                f"{self.base_url}/t/{topic_id}.json",
                headers=self.headers,
                verify=self.verify_ssl
            )
            return response.json()['post_stream']['posts']
        except Exception as e:
            log.error(f"Error getting topic posts: {str(e)}")
            return []

    def create_topic(self, title, raw, category_id, tags=None):
        payload = {
            'title': title,
            'raw': raw,
            'category': category_id,
            'tags': tags or [],
            'skip_validations': True
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/posts.json",
                headers=self.headers,
                json=payload,
                verify=self.verify_ssl
            )
            return response.json()
        except Exception as e:
            log.error(f"Error creating topic: {str(e)}")
            return None

    def update_post(self, post_id, raw):
        payload = {
            'post': {'raw': raw}
        }
        
        try:
            response = requests.put(
                f"{self.base_url}/posts/{post_id}.json",
                headers=self.headers,
                json=payload,
                verify=self.verify_ssl
            )
            return response.json()
        except Exception as e:
            log.error(f"Error updating post: {str(e)}")
            return None