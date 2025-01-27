import json
import requests
import logging

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 5

class DiscourseApi:
    """
    Helper class for interacting with the Discourse API.
    """

    def __init__(self, discourse_url, api_username, api_key):
        self.discourse_url = discourse_url.rstrip('/')
        self.api_username = api_username
        self.api_key = api_key
        self.headers = {
            'Api-Key': self.api_key,
            'Api-Username': self.api_username,
            'Content-Type': 'application/json'
        }
        log.debug(f"Discourse API initialized for URL: {self.discourse_url}")

    def create_topic(self, title, raw, category_id):
        """
        Creates a new topic in Discourse.
        """
        url = '{0}/posts.json'.format(self.discourse_url)
        payload = {
            'title': title,
            'raw': raw,
            'category': category_id,
            'skip_validations': 'true'
        }
        log.debug(f"Creating Discourse topic: URL={url}, Payload={payload}")
        try:
            r = requests.post(
                url,
                headers=self.headers,
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
                verify=False
            )
            r.raise_for_status()
            log.info(f'Discourse topic created: {title}')
            return r.json()
        except requests.exceptions.RequestException as e:
            log.error(f'Error creating Discourse topic: {e}')
            return None

    def get_topic_list(self, category_id):
        """
        Retrieves a list of topics from a Discourse category.
        """
        topics = []
        page = 0
        while True:
            url = '{0}/c/{1}.json?page={2}'.format(self.discourse_url, category_id, page)
            log.debug(f"Getting Discourse topic list: URL={url}")
            try:
                r = requests.get(
                    url,
                    headers=self.headers,
                    timeout=REQUEST_TIMEOUT,
                    verify=False
                )
                r.raise_for_status()
                category = r.json()
                if category.get('topic_list', {}).get('topics'):
                    topics.extend(category.get('topic_list', {}).get('topics'))
                    page += 1
                else:
                    break
            except requests.exceptions.RequestException as e:
                log.error(f'Error getting Discourse topic list: {e}')
                break
        return topics

    def get_topic_posts(self, topic_id):
        """
        Retrieves posts from a Discourse topic.
        """
        url = '{0}/t/{1}.json'.format(self.discourse_url, topic_id)
        log.debug(f"Getting Discourse topic posts: URL={url}")
        try:
            r = requests.get(
                url,
                headers=self.headers,
                timeout=REQUEST_TIMEOUT,
                verify=False
            )
            r.raise_for_status()
            return r.json()['post_stream']['posts']
        except requests.exceptions.RequestException as e:
            log.error(f'Error getting Discourse topic posts: {e}')
            return []

    def update_post(self, post_id, raw):
        """
        Updates a post in a Discourse topic.
        """
        url = '{0}/posts/{1}.json'.format(self.discourse_url, post_id)
        payload = {
            'post': {
                'raw': raw
            }
        }
        log.debug(f"Updating Discourse post: URL={url}, Payload={payload}")
        try:
            r = requests.put(
                url,
                headers=self.headers,
                data=json.dumps(payload),
                timeout=REQUEST_TIMEOUT,
                verify=False
            )
            r.raise_for_status()
            log.info(f'Discourse post updated: {post_id}')
            return r.json()
        except requests.exceptions.RequestException as e:
            log.error(f'Error updating Discourse post: {e}')
            return None