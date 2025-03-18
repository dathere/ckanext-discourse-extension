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
    
    # Class variable to store discourse extras by dataset ID
    _discourse_extras_cache = {}
    
    # Class variable to track packages being updated to prevent duplicate jobs
    _updating_packages = set()

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

    def edit(self, entity):
        """Called after package has been updated.
        
        This is a better hook to use since it's called by CKAN core
        when a package is edited, right after the database commit.
        """
        pass
        
    def after_dataset_update(self, context, pkg_dict):
        """Queue topic update job and preserve discourse extras."""
        pkg_id = pkg_dict['id']
        
        # Skip if we're already updating this package (prevent recursive updates)
        if pkg_id in self._updating_packages:
            log.info(f"Skipping duplicate update for dataset {pkg_id} - already in progress")
            return
            
        try:
            # Mark this package as being updated
            self._updating_packages.add(pkg_id)
            
            # First try to get the topic ID directly from extras
            context_show = {'ignore_auth': True}
            try:
                full_pkg = get_action('package_show')(context_show, {'id': pkg_id})
                extras_dict = self._get_extras_dict(full_pkg)
                topic_id = extras_dict.get('discourse_topic_id')
                discourse_url = extras_dict.get('discourse_url')
                
                log.info(f"Retrieved topic_id from package: {topic_id}")
            except Exception as e:
                log.error(f"Error retrieving package: {str(e)}")
                topic_id = None
                discourse_url = None
            
            # If we couldn't find the topic ID in the current extras, check if it exists in the cache
            if not topic_id and pkg_id in self._discourse_extras_cache:
                log.info(f"Retrieving discourse extras from cache for {pkg_id}")
                cached_extras = self._discourse_extras_cache[pkg_id]
                topic_id = cached_extras.get('discourse_topic_id')
                discourse_url = cached_extras.get('discourse_url')
                
                if topic_id:
                    log.info(f"Found cached topic_id {topic_id} for dataset {pkg_id}")
            
            # Store current extras in the cache for future reference
            if topic_id:
                log.info(f"Storing discourse extras in cache for {pkg_id}")
                self._discourse_extras_cache[pkg_id] = {
                    'discourse_topic_id': topic_id,
                    'discourse_url': discourse_url
                }
                
                # If we have a topic ID, schedule the update - but only once
                if self.settings and not pkg_dict.get('private'):
                    log.info(f"Scheduling update for Discourse topic {topic_id} for dataset {pkg_id}")
                    
                    # Create a simplified dict with just the necessary data
                    # Don't include any database objects or nested structures that could cause issues
                    update_data = {
                        'id': pkg_id,
                        'name': pkg_dict.get('name'),
                        'title': pkg_dict.get('title', ''),
                        'notes': pkg_dict.get('notes', ''),
                        'discourse_topic_id': topic_id,
                        'discourse_url': discourse_url
                    }
                    
                    # Copy resources if they exist
                    if 'resources' in pkg_dict:
                        update_data['resources'] = []
                        for res in pkg_dict.get('resources', []):
                            update_data['resources'].append({
                                'id': res.get('id'),
                                'name': res.get('name', ''),
                                'url': res.get('url', '')
                            })
                    
                    # Add other metadata fields
                    if self.settings.get('metadata_fields'):
                        for field in self.settings['metadata_fields']:
                            if field in pkg_dict:
                                update_data[field] = pkg_dict[field]
                    
                    # Schedule the job with our simplified data
                    enqueue(
                        update_discourse_topic, 
                        args=[update_data, self.settings, topic_id],
                        title=f"Update Discourse topic for {pkg_id}"
                    )
            else:
                log.info(f"No discourse_topic_id found for dataset {pkg_id}, cannot update")
                
        except Exception as e:
            log.error(f"Error preparing dataset update in Discourse: {str(e)}", exc_info=True)
        finally:
            # Always make sure to remove the package from the updating set
            if pkg_id in self._updating_packages:
                self._updating_packages.remove(pkg_id)
                log.debug(f"Removed {pkg_id} from updating packages set")

    def discourse_comments(self, pkg_dict=None):
        """Render comments section."""
        try:
            pkg = pkg_dict or toolkit.g.pkg_dict
            pkg_id = pkg.get('id')
            
            if not pkg_id:
                return '<p class="text-muted">Package ID not available</p>'
            
            # First try to get topic_id from package extras
            extras_dict = self._get_extras_dict(pkg) 
            topic_id = extras_dict.get('discourse_topic_id')
            discourse_url = extras_dict.get('discourse_url', self.settings['url'] if self.settings else '')
            
            log.debug(f"Rendering comments for package: {pkg.get('name')}")
            log.debug(f"Package extras: {extras_dict}")
            log.debug(f"Found topic ID from extras: {topic_id}")
            
            # If no topic_id in extras, check if we have it in our cache
            if not topic_id and pkg_id in self._discourse_extras_cache:
                cached_extras = self._discourse_extras_cache[pkg_id]
                topic_id = cached_extras.get('discourse_topic_id')
                discourse_url = cached_extras.get('discourse_url', self.settings['url'] if self.settings else '')
                
                log.debug(f"Found topic ID from cache: {topic_id}")
                
                # Don't try to update the database here - just use the cached value
                
            if not topic_id:
                return '<p class="text-muted">Comments will be available after refreshing this page</p>'
            
            # Store the topic_id in cache for future reference
            if pkg_id and topic_id:
                self._discourse_extras_cache[pkg_id] = {
                    'discourse_topic_id': topic_id,
                    'discourse_url': discourse_url
                }
                log.debug(f"Stored discourse extras in cache for {pkg_id}")
            
            return toolkit.render_snippet('discourse_comments.html', { 
                'discourse_url': self.settings['url'] if self.settings else discourse_url,
                'topic_id': topic_id
            })
        except Exception as e:
            log.error(f"Error rendering comments: {str(e)}", exc_info=True)
            return '<p class="text-muted">Error loading comments</p>'

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
        
        try:
            response = requests.post(
                url,
                headers=self.headers,
                json=data,
                verify=self.verify_ssl,
                timeout=10  # Add timeout to prevent hanging
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"Discourse API error in create_topic: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                log.error(f"Response status: {e.response.status_code}, text: {e.response.text}")
            raise

    def update_topic(self, topic_id, content):
        """Update the first post of a topic."""
        try:
            post_id = self._get_first_post_id(topic_id)
            url = f"{self.base_url}/posts/{post_id}"
            data = {'post': {'raw': content}}
            
            response = requests.put(
                url,
                headers=self.headers,
                json=data,
                verify=self.verify_ssl,
                timeout=10  # Add timeout to prevent hanging
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log.error(f"Discourse API error in update_topic: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                log.error(f"Response status: {e.response.status_code}, text: {e.response.text}")
            raise

    def get_topic_comment_count(self, topic_id):
        """Get total posts count excluding the initial post."""
        try:
            url = f"{self.base_url}/t/{topic_id}.json"
            response = requests.get(
                url, 
                headers=self.headers,
                verify=self.verify_ssl,
                timeout=5  # Add timeout to prevent hanging
            )
            response.raise_for_status()
            return response.json()['posts_count'] - 1
        except requests.exceptions.RequestException as e:
            log.error(f"Discourse API error in get_topic_comment_count: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                log.error(f"Response status: {e.response.status_code}, text: {e.response.text}")
            return 0  # Return 0 on error to avoid breaking the UI

    def _get_first_post_id(self, topic_id):
        """Retrieve the first post ID of a topic."""
        try:
            url = f"{self.base_url}/t/{topic_id}/posts.json"
            response = requests.get(
                url,
                headers=self.headers,
                verify=self.verify_ssl,
                timeout=5  # Add timeout to prevent hanging
            )
            response.raise_for_status()
            return response.json()['post_stream']['posts'][0]['id']
        except requests.exceptions.RequestException as e:
            log.error(f"Discourse API error in _get_first_post_id: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                log.error(f"Response status: {e.response.status_code}, text: {e.response.text}")
            raise
        except (KeyError, IndexError) as e:
            log.error(f"Error parsing Discourse API response in _get_first_post_id: {str(e)}")
            raise ValueError(f"Could not extract post ID from topic {topic_id}: {str(e)}")
 
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
        try:
            result = api.create_topic(pkg_dict['title'], content, settings['category_id'])
            topic_id = result['topic_id']
            topic_url = f"{settings['url']}/t/{topic_id}"
            log.info(f"Successfully created Discourse topic {topic_id}")
            
            # 2. Update CKAN package
            context = {'ignore_auth': True}
            try:
                # Get a clean package with minimal data to update
                update_dict = {
                    'id': pkg_dict['id'],
                    'extras': [
                        {'key': 'discourse_topic_id', 'value': str(topic_id)},
                        {'key': 'discourse_url', 'value': topic_url}
                    ]
                }
                
                log.debug(f"Updating package {pkg_dict['id']} with Discourse topic ID {topic_id}")
                get_action('package_patch')(context, update_dict)
                log.info(f"Successfully updated package {pkg_dict['id']} with Discourse topic ID {topic_id}")
                
            except Exception as update_error:
                log.error(f"Error updating package with Discourse topic ID: {str(update_error)}", exc_info=True)
                
        except Exception as api_error:
            log.error(f"Discourse API error creating topic: {str(api_error)}", exc_info=True)
            if hasattr(api_error, 'response') and api_error.response is not None:
                log.error(f"API response: {api_error.response.text}")
            
    except Exception as e:
        log.error(f"Unexpected error in topic creation: {str(e)}", exc_info=True)

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
