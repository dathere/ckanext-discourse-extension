import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
import ckan.logic as logic
import requests
import json
import logging
from ckan.common import config
from ckan.lib.jobs import enqueue
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)
get_action = logic.get_action

class DiscoursePlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurable)
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.ITemplateHelpers)
    plugins.implements(plugins.IBlueprint)

    def configure(self, config_):
        """Load and validate configuration settings."""
        self.settings = {
            'url': config_.get('discourse.url', '').rstrip('/'),
            'api_key': config_.get('discourse.api_key'),
            'username': config_.get('discourse.username'),
            'category_id': config_.get('discourse.category_id'),
            'site_category_id': config_.get('discourse.site_category_id'),
            'metadata_fields': config_.get('discourse.metadata_fields', '').split(),
            'verify_ssl': toolkit.asbool(config_.get('discourse.verify_ssl', True))
        }

        if not self._validate_config():
            log.error("Discourse integration disabled due to invalid configuration")
            self.settings = None

    def _validate_config(self):
        """Check required configuration settings."""
        required = ['url', 'api_key', 'username', 'category_id']
        return all(self.settings.get(key) for key in required)

    def update_config(self, config_):
        """Add template directory."""
        toolkit.add_template_directory(config_, 'templates')
        toolkit.add_resource('assets', 'discourse')

    def get_helpers(self):
        """Register template helpers."""
        return {
            'discourse_comments': self.discourse_comments,
            'discourse_comments_count': self.discourse_comments_count,
            'discourse_is_configured': self.discourse_is_configured,
            'discourse_has_topic': self.discourse_has_topic,
            'current_user': self._get_current_user,
        }
    
    def get_blueprint(self):
        """Register Blueprint for handling discourse actions."""
        blueprint = Blueprint('discourse', __name__)
        
        blueprint.add_url_rule(
            '/discourse/create_topic/<id>',
            'create_topic',
            self.create_topic,
            methods=['GET', 'POST']
        )
        
        return blueprint
    
    def create_topic(self, id):
        """Handle the creation of a discourse topic for a dataset."""
        if not self.settings:
            return jsonify({'success': False, 'error': 'Discourse not configured'})
            
        # Check if user is logged in
        if not toolkit.g.user:
            return jsonify({'success': False, 'error': 'You must be logged in to start a discussion'})
            
        try:
            # Get the package
            context = {'user': toolkit.g.user, 'auth_user_obj': toolkit.g.userobj}
            pkg_dict = get_action('package_show')(context, {'id': id})
            
            # Check if package already has a topic
            if pkg_dict.get('topic_id'):
                return jsonify({'success': False, 'error': 'Topic already exists'})
                
            # Create topic in Discourse
            result = create_discourse_topic(pkg_dict, self.settings)
            
            if result and result.get('topic_id'):
                try:
                    # Create a system context to update the package regardless of user permissions
                    sysadmin_context = {
                        'ignore_auth': True,
                        'user': toolkit.g.user
                    }
                    
                    # Update package with topic_id in metadata schema
                    pkg_dict['topic_id'] = str(result['topic_id'])
                    get_action('package_update')(sysadmin_context, pkg_dict)
                    
                    return jsonify({
                        'success': True, 
                        'topic_id': result['topic_id'],
                        'topic_url': result['topic_url']
                    })
                except logic.NotAuthorized:
                    # If the user can't update the package, we need to handle this specially
                    log.warning(f"User {toolkit.g.user} started discourse topic but couldn't update package {id}")
                    return jsonify({
                        'success': True,
                        'topic_id': result['topic_id'],
                        'topic_url': result['topic_url'],
                        'warning': 'Topic created but package metadata could not be updated'
                    })
            else:
                return jsonify({'success': False, 'error': 'Failed to create topic'})
                
        except Exception as e:
            log.error(f"Error creating topic: {str(e)}")
            return jsonify({'success': False, 'error': str(e)})

    def discourse_comments(self, pkg_dict=None):
        """Render comments section."""
        try:
            pkg = pkg_dict or getattr(toolkit.g, 'pkg_dict', None)
            if not pkg:
                return ''
            topic_id = pkg.get('topic_id')
            
            if not (self.settings and topic_id):
                # Render the button to start discussions
                return toolkit.render_snippet('discourse_start_button.html', {
                    'pkg_id': pkg.get('id'),
                    'has_topic': bool(topic_id)
                })
            
            return toolkit.render_snippet('discourse_comments.html', { 
                'discourse_url': self.settings['url'],
                'topic_id': topic_id
            })
        except Exception as e:
            log.error(f"Error rendering comments: {str(e)}", exc_info=True)
            return ''

    def discourse_comments_count(self, pkg_dict=None):
        """Get comment count from Discourse."""
        try:
            pkg = pkg_dict or getattr(toolkit.g, 'pkg_dict', None)
            if not pkg:
                return 0
            topic_id = pkg.get('topic_id')
            
            if not (self.settings and topic_id):
                return 0
                
            api = DiscourseApi(self.settings)
            count = api.get_topic_comment_count(topic_id)
            return count
            
        except Exception as e:
            log.error(f"Error getting comment count: {str(e)}")
            return 0
    
    def discourse_is_configured(self):
        """Check if Discourse is properly configured."""
        return self.settings is not None
        
    def discourse_has_topic(self, pkg_dict=None):
        """Check if package has a Discourse topic."""
        pkg = pkg_dict or getattr(toolkit.g, 'pkg_dict', None)
        if not pkg:
            return False
        return bool(pkg.get('topic_id'))
        
    def _get_current_user(self):
        """Helper method to check if a user is logged in."""
        try:
            return toolkit.g.user
        except (AttributeError, TypeError):
            return None

class DiscourseApi:
    """Discourse API client with proper configuration handling."""
    
    def __init__(self, settings):
        self.base_url = settings['url']
        self.headers = {
            'Api-Key': settings['api_key'],
            'Api-Username': settings['username'],
            'Content-Type': 'application/json'
        }
        self.verify_ssl = settings['verify_ssl']

    def create_topic(self, title, content, category_id):
        """Create a new Discourse topic."""
        url = f"{self.base_url}/posts.json"
        data = {
            'title': title,
            'raw': content,
            'category': category_id,
            'skip_validations': 'true'
        }
        
        response = requests.post(
            url,
            headers=self.headers,
            json=data,
            verify=self.verify_ssl
        )
        response.raise_for_status()
        return response.json()

    def update_topic(self, topic_id, content):
        """Update the first post of a topic."""
        post_id = self._get_first_post_id(topic_id)
        url = f"{self.base_url}/posts/{post_id}"
        data = {'post': {'raw': content}}
        
        response = requests.put(
            url,
            headers=self.headers,
            json=data,
            verify=self.verify_ssl
        )
        response.raise_for_status()
        return response.json()

    def get_topic_comment_count(self, topic_id):
        """Get total posts count excluding the initial post."""
        url = f"{self.base_url}/t/{topic_id}.json"
        response = requests.get(
            url, 
            headers=self.headers,
            verify=self.verify_ssl
        )
        response.raise_for_status()
        return response.json()['posts_count'] - 1

    def _get_first_post_id(self, topic_id):
        """Retrieve the first post ID of a topic."""
        url = f"{self.base_url}/t/{topic_id}/posts.json"
        response = requests.get(
            url,
            headers=self.headers,
            verify=self.verify_ssl
        )
        response.raise_for_status()
        return response.json()['post_stream']['posts'][0]['id']
 
def create_discourse_topic(pkg_dict, settings):
    """Create Discourse topic for a dataset."""
    log.info(f"Creating topic for dataset: {pkg_dict['id']}")
    
    try:
        # Create Discourse topic
        api = DiscourseApi(settings)
        site_url = config['ckan.site_url'].rstrip('/')
        pkg_url = f"{site_url}/dataset/{pkg_dict['name']}" 
        
        # Generate content
        # Remove extra newlines and ensure proper markdown formatting
        detailed_info = pkg_dict.get('detailed_info', pkg_dict.get('notes', '')).strip()
        # Remove any existing heading markers from detailed_info to avoid conflicts
        detailed_info = '\n'.join(line for line in detailed_info.splitlines() 
                                if not line.strip().startswith('#'))

        content = f"# {pkg_dict['title']}\n\n{detailed_info}\n\n"
        content += f"**Dataset URL**: [View on CKAN]({pkg_url})\n\n"
        for field in settings['metadata_fields']:
            if value := pkg_dict.get(field):
                content += f"**{field.title()}**: {value}\n"
        
        # Choose the appropriate category based on dataset type
        category_id = settings.get('site_category_id') if pkg_dict.get('type') == 'site' else settings['category_id']
        
        result = api.create_topic(pkg_dict['title'], content, category_id)
        topic_id = result['topic_id']
        topic_url = f"{settings['url']}/t/{topic_id}"
        log.info(f"Successfully created Discourse topic {topic_id} in category {category_id}")
        
        return {
            'topic_id': topic_id,
            'topic_url': topic_url
        }

    except requests.exceptions.RequestException as e:
        log.error(f"Discourse API error: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            log.error(f"API response: {e.response.text}")
        return None
    except Exception as e:
        log.error(f"Unexpected error in topic creation: {str(e)}")
        return None
