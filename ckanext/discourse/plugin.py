import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit
import ckan.logic as logic
import requests
import json
import logging
from ckan.common import config
from ckan.lib.jobs import enqueue

log = logging.getLogger(__name__)
get_action = logic.get_action

class DiscoursePlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurable)
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.ITemplateHelpers)
    plugins.implements(plugins.IPackageController, inherit=True)
    
    # Store discourse info temporarily during updates
    _preserved_discourse_info = {}

    def configure(self, config_):
        """Load and validate configuration settings."""
        self.settings = {
            'url': config_.get('discourse.url', '').rstrip('/'),
            'api_key': config_.get('discourse.api_key'),
            'username': config_.get('discourse.username'),
            'category_id': config_.get('discourse.category_id'),
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
        }

    def after_dataset_create(self, context, pkg_dict):
        """Queue topic creation job."""
        if self.settings and not pkg_dict.get('private'):
            enqueue(
                create_discourse_topic,
                args=[pkg_dict, self.settings],
                title=f"Create Discourse topic for {pkg_dict['id']}"
            )

    def before_update(self, context, pkg_dict, is_org_update):
        """Preserve discourse information before an update occurs."""
        try:
            # This needs to happen BEFORE the update to save the current discourse info
            pkg_id = pkg_dict.get('id')
            if not pkg_id:
                return pkg_dict
                
            log.info(f"Preserving discourse info before update for dataset {pkg_id}")
            
            # Get the original package with all its extras
            context_show = {'ignore_auth': True}
            original_pkg = get_action('package_show')(context_show, {'id': pkg_id})
            
            # Extract discourse extras
            extras_dict = self._get_extras_dict(original_pkg)
            topic_id = extras_dict.get('discourse_topic_id')
            discourse_url = extras_dict.get('discourse_url')
            
            if topic_id:
                log.info(f"Preserved discourse_topic_id {topic_id} for dataset {pkg_id}")
                # Store this information for use after the update
                self._preserved_discourse_info[pkg_id] = {
                    'discourse_topic_id': topic_id,
                    'discourse_url': discourse_url
                }
            else:
                log.info(f"No discourse_topic_id found to preserve for dataset {pkg_id}")
                
            return pkg_dict
        except Exception as e:
            log.error(f"Error preserving discourse info: {str(e)}", exc_info=True)
            return pkg_dict
            
    def after_dataset_update(self, context, pkg_dict):
        """Queue topic update job and ensure discourse info is preserved."""
        pkg_id = pkg_dict.get('id')
        if not pkg_id:
            return
            
        try:
            # First, check if we need to restore discourse info that might have been lost during the update
            preserved_info = self._preserved_discourse_info.get(pkg_id)
            
            # Get the updated package to see if the discourse info is still there
            context_show = {'ignore_auth': True}
            updated_pkg = get_action('package_show')(context_show, {'id': pkg_id})
            extras_dict = self._get_extras_dict(updated_pkg)
            
            topic_id = extras_dict.get('discourse_topic_id')
            discourse_url = extras_dict.get('discourse_url')
            
            # If we lost the discourse info but have preserved it, restore it
            if preserved_info and not topic_id:
                log.info(f"Discourse info was lost during update, restoring for dataset {pkg_id}")
                
                # Update the package with the preserved discourse info
                preserved_topic_id = preserved_info.get('discourse_topic_id')
                preserved_url = preserved_info.get('discourse_url')
                
                # Process extras for update
                extras_list = updated_pkg.get('extras', [])
                extras_dict = {e['key']: e for e in extras_list} if isinstance(extras_list, list) else {}
                
                # Add the preserved discourse info back
                if preserved_topic_id:
                    if 'discourse_topic_id' in extras_dict:
                        extras_dict['discourse_topic_id']['value'] = preserved_topic_id
                    else:
                        extras_dict['discourse_topic_id'] = {'key': 'discourse_topic_id', 'value': preserved_topic_id}
                        
                if preserved_url:
                    if 'discourse_url' in extras_dict:
                        extras_dict['discourse_url']['value'] = preserved_url
                    else:
                        extras_dict['discourse_url'] = {'key': 'discourse_url', 'value': preserved_url}
                
                # Update the package with restored extras
                updated_pkg['extras'] = list(extras_dict.values())
                context_update = {'ignore_auth': True}
                get_action('package_update')(context_update, updated_pkg)
                
                log.info(f"Successfully restored discourse info for dataset {pkg_id}")
                
                # Use the preserved topic_id for further operations
                topic_id = preserved_topic_id
                
                # Clean up the preserved info
                if pkg_id in self._preserved_discourse_info:
                    del self._preserved_discourse_info[pkg_id]
            
            log.info(f"After update for dataset {pkg_id}, topic_id: {topic_id}")
            
            # Now proceed with updating the discourse topic
            if self.settings and not updated_pkg.get('private') and topic_id:
                log.info(f"Scheduling update for Discourse topic {topic_id} for dataset {pkg_id}")
                # Use the updated package with restored discourse info
                enqueue(
                    update_discourse_topic,
                    args=[updated_pkg, self.settings],
                    title=f"Update Discourse topic for {pkg_id}"
                )
            else:
                if not topic_id:
                    log.info(f"No discourse_topic_id found for dataset {pkg_id}, cannot update")
                if updated_pkg.get('private'):
                    log.info(f"Dataset {pkg_id} is private, skipping update")
                if not self.settings:
                    log.info("Discourse settings not available, skipping update")
        except Exception as e:
            log.error(f"Error in after_dataset_update: {str(e)}", exc_info=True)

    def discourse_comments(self, pkg_dict=None):
        """Render comments section."""
        try:
            pkg = pkg_dict or toolkit.g.pkg_dict
            pkg_id = pkg.get('id')
            extras_dict = self._get_extras_dict(pkg) 
            topic_id = extras_dict.get('discourse_topic_id')
            discourse_url = extras_dict.get('discourse_url', self.settings['url'] if self.settings else '')
            
            # Check if we have preserved info that might not be in the extras yet
            if not topic_id and pkg_id and pkg_id in self._preserved_discourse_info:
                preserved_info = self._preserved_discourse_info.get(pkg_id, {})
                topic_id = preserved_info.get('discourse_topic_id')
                discourse_url = preserved_info.get('discourse_url', self.settings['url'] if self.settings else '')
                log.debug(f"Using preserved topic ID: {topic_id} for package: {pkg.get('name')}")
            
            log.debug(f"Rendering comments for package: {pkg.get('name')}")
            log.debug(f"Package extras: {extras_dict}")
            log.debug(f"Found topic ID: {topic_id}")

            if not topic_id:
                return '<p class="text-muted">Comments will be available after refreshing this page</p>'
            
            return toolkit.render_snippet('discourse_comments.html', { 
                'discourse_url': self.settings['url'] if self.settings else discourse_url,
                'topic_id': topic_id
            })
        except Exception as e:
            log.error(f"Error rendering comments: {str(e)}", exc_info=True)
            return ''

    def discourse_comments_count(self, pkg_dict=None):
        """Get comment count from Discourse."""
        try:
            pkg = pkg_dict or toolkit.g.pkg_dict
            extras_dict = self._get_extras_dict(pkg)
            topic_id = extras_dict.get('discourse_topic_id')
            
            if not (self.settings and topic_id):
                return 0
                
            log.debug(f"Getting comment count for topic: {topic_id}")
            api = DiscourseApi(self.settings)
            count = api.get_topic_comment_count(topic_id)
            log.debug(f"Comment count retrieved: {count}")
            return count
            
        except Exception as e:
            log.error(f"Error getting comment count: {str(e)}", exc_info=True)
            return 0

    def _get_extras_dict(self, pkg):
        """Convert extras list to dictionary."""
        try:
            if isinstance(pkg.get('extras'), list):
                return {item['key']: item['value'] for item in pkg['extras']}
            if isinstance(pkg.get('extras'), dict):
                return pkg['extras']
            return {}
        except Exception as e:
            log.error(f"Error processing extras: {str(e)}")
            return {}

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
    """Background job: Create Discourse topic and update dataset."""
    log.info(f"Starting topic creation for dataset: {pkg_dict['id']}")
    
    try:
        # 1. Create Discourse topic
        api = DiscourseApi(settings)
        site_url = config['discourse.site_url'].rstrip('/')
        pkg_url = f"{site_url}/dataset/{pkg_dict['name']}" 
        
        # Generate content
        content = f"## {pkg_dict['title']}\n\n{pkg_dict.get('notes', '')}\n\n"
        content += f"**Dataset URL**: [View on CKAN]({pkg_url})\n\n"
        for field in settings['metadata_fields']:
            if value := pkg_dict.get(field):
                content += f"**{field.title()}**: {value}\n"
        if resources := pkg_dict.get('resources'):
            content += "\n**Resources:**\n"
            for res in resources:
                res_url = f"{pkg_url}/resource/{res['id']}"
                content += f"- [{res['name']}]({res_url})\n"

        log.debug(f"Creating topic with content: {content[:200]}...")
        result = api.create_topic(pkg_dict['title'], content, settings['category_id'])
        topic_id = result['topic_id']
        topic_url = f"{settings['url']}/t/{topic_id}"
        log.info(f"Successfully created Discourse topic {topic_id}")

        # 2. Update CKAN package
        context = {'ignore_auth': True}
        try:
            log.debug(f"Fetching package {pkg_dict['id']} for update")
            pkg = get_action('package_show')(context, {'id': pkg_dict['id']})
            
            # Process extras
            extras_dict = {e['key']: e for e in pkg.get('extras', [])}
            new_extras = {
                'discourse_topic_id': str(topic_id),
                'discourse_url': topic_url
            }
            
            for key, value in new_extras.items():
                if key in extras_dict:
                    log.debug(f"Updating existing extra: {key}")
                    extras_dict[key]['value'] = value
                else:
                    log.debug(f"Adding new extra: {key}")
                    extras_dict[key] = {'key': key, 'value': value}
            
            pkg['extras'] = list(extras_dict.values())
            log.debug(f"Final extras to save: {pkg['extras']}")

            updated_pkg = get_action('package_update')(context, pkg)
            log.info(f"Successfully updated package {pkg_dict['id']} with Discourse info")
            log.debug(f"Updated package extras: {updated_pkg.get('extras')}")
            
        except logic.NotFound as e:
            log.error(f"Package not found: {pkg_dict['id']} - {str(e)}")
        except logic.ValidationError as e:
            log.error(f"Validation error updating package: {e.error_dict}")
        except Exception as e:
            log.error(f"Unexpected error updating package: {str(e)}")

    except requests.exceptions.RequestException as e:
        log.error(f"Discourse API error: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            log.error(f"API response: {e.response.text}")
    except Exception as e:
        log.error(f"Unexpected error in topic creation: {str(e)}")

def update_discourse_topic(pkg_dict, settings):
    """Background job: Update existing Discourse topic."""
    try:
        log.info(f"Starting topic update for dataset: {pkg_dict['id']}")
        
        # Get topic_id from package extras
        extras_dict = {}
        if isinstance(pkg_dict.get('extras'), list):
            extras_dict = {item['key']: item['value'] for item in pkg_dict['extras']}
        elif isinstance(pkg_dict.get('extras'), dict):
            extras_dict = pkg_dict['extras']
        
        topic_id = extras_dict.get('discourse_topic_id')
        
        if not topic_id:
            log.error(f"Cannot update Discourse topic: no topic_id found for package {pkg_dict['id']}")
            return
            
        log.info(f"Found topic_id {topic_id} for dataset {pkg_dict['id']}")
        
        api = DiscourseApi(settings)
        site_url = config['discourse.site_url'].rstrip('/')
        pkg_url = f"{site_url}/dataset/{pkg_dict['name']}"
        
        content = f"## {pkg_dict['title']}\n\n{pkg_dict.get('notes', '')}\n\n"
        content += f"**Dataset URL**: [View on CKAN]({pkg_url})\n\n"
        
        # Add metadata fields
        for field in settings['metadata_fields']:
            if value := pkg_dict.get(field):
                content += f"**{field.title()}**: {value}\n"
        
        # Add resources
        if resources := pkg_dict.get('resources'):
            content += "\n**Resources:**\n"
            for res in resources:
                res_url = f"{pkg_url}/resource/{res['id']}"
                content += f"- [{res['name']}]({res_url})\n"

        log.debug(f"Updating topic {topic_id} with content: {content[:200]}...")
        result = api.update_topic(topic_id, content)
        log.info(f"Successfully updated Discourse topic {topic_id}")
        
        # Ensure the extras are preserved in the package
        context = {'ignore_auth': True}
        try:
            # Get the latest version of the package
            latest_pkg = get_action('package_show')(context, {'id': pkg_dict['id']})
            
            # Process extras to ensure discourse info is preserved
            latest_extras_dict = {e['key']: e for e in latest_pkg.get('extras', [])}
            preserved_extras = {
                'discourse_topic_id': str(topic_id),
                'discourse_url': extras_dict.get('discourse_url', f"{settings['url']}/t/{topic_id}")
            }
            
            need_update = False
            for key, value in preserved_extras.items():
                if key not in latest_extras_dict or latest_extras_dict[key]['value'] != value:
                    if key in latest_extras_dict:
                        log.debug(f"Updating existing extra: {key}")
                        latest_extras_dict[key]['value'] = value
                    else:
                        log.debug(f"Adding new extra: {key}")
                        latest_extras_dict[key] = {'key': key, 'value': value}
                    need_update = True
            
            # Only update if needed
            if need_update:
                latest_pkg['extras'] = list(latest_extras_dict.values())
                log.debug(f"Ensuring discourse extras are preserved: {latest_pkg['extras']}")
                updated_pkg = get_action('package_update')(context, latest_pkg)
                log.info(f"Successfully ensured discourse extras for package {pkg_dict['id']}")
            
        except Exception as e:
            log.error(f"Error ensuring discourse extras: {str(e)}", exc_info=True)

    except requests.exceptions.RequestException as e:
        log.error(f"Discourse API error updating topic: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            log.error(f"API response: {e.response.text}")
    except Exception as e:
        log.error(f"Unexpected error in topic update: {str(e)}", exc_info=True)
