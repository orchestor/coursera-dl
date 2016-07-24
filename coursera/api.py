# vim: set fileencoding=utf8 :
"""
This module contains implementations of different APIs that are used by the
downloader.
"""

import os
import json
import base64
import logging
from six import iterkeys, iteritems
from six.moves.urllib_parse import quote_plus

from .utils import (BeautifulSoup, make_coursera_absolute_url,
                    extend_supplement_links, clean_url, clean_filename)
from .network import get_page, get_page_json, post_page_and_reply, post_page_json
from .define import (OPENCOURSE_SUPPLEMENT_URL,
                     OPENCOURSE_PROGRAMMING_ASSIGNMENTS_URL,
                     OPENCOURSE_ASSET_URL,
                     OPENCOURSE_ASSETS_URL,
                     OPENCOURSE_API_ASSETS_V1_URL,
                     OPENCOURSE_ONDEMAND_COURSE_MATERIALS,
                     OPENCOURSE_VIDEO_URL,
                     OPENCOURSE_MEMBERSHIPS,
                     POST_OPENCOURSE_API_QUIZ_SESSION,
                     POST_OPENCOURSE_API_QUIZ_SESSION_GET_STATE,
                     POST_OPENCOURSE_ONDEMAND_EXAM_SESSIONS,
                     POST_OPENCOURSE_ONDEMAND_EXAM_SESSIONS_GET_STATE,

                     INSTRUCTIONS_HTML_INJECTION,

                     IN_MEMORY_EXTENSION,
                     IN_MEMORY_MARKER)


from .cookies import prepape_auth_headers


class QuizExamToMarkupConverter(object):
    """
    Converts quiz/exam JSON into semi HTML (Coursera Markup) for local viewing.
    The output needs to be further processed by MarkupToHTMLConverter.
    """
    KNOWN_QUESTION_TYPES = ('mcq',
                            'checkbox',
                            'singleNumeric',
                            'textExactMatch')

    KNOWN_INPUT_TYPES = ('textExactMatch',
                         'singleNumeric')

    def __init__(self, session):
        self._session = session

    def __call__(self, quiz_or_exam_json):
        result = []

        for question_index, question_json in enumerate(quiz_or_exam_json['questions']):
            question_type = question_json['question']['type']
            if question_type not in self.KNOWN_QUESTION_TYPES:
                logging.info('Unknown question type: %s', question_type)
                logging.info('Question json: %s', question_json)
                logging.info('Please report class name, quiz name and the data'
                             ' above to coursera-dl authors')
            print('QUESTION TYPE', question_type)

            prompt = question_json['variant']['definition']['prompt']
            options = question_json['variant']['definition'].get('options', [])

            # Question number
            result.append('<h3>Question %d</h3>' % (question_index + 1))

            # Question text
            question_text = prompt['definition']['value']
            result.append(question_text)

            # Input for answer
            if question_type in self.KNOWN_INPUT_TYPES:
                result.extend(self._generate_input_field())

            # Convert input_type form JSON reply to HTML input type
            input_type = {
                'mcq': 'radio',
                'checkbox': 'checkbox'
            }.get(question_type, '')

            # Convert options, they are either checkboxes or radio buttons
            result.extend(self._convert_options(
                question_index, options, input_type))

            result.append('<hr>')

        return '\n'.join(result)
        # prettifier = InstructionsPrettifier(self._session)
        # return prettifier.prettify('\n'.join(result))

    def _convert_options(self, question_index, options, input_type):
        if not options:
            return []

        result = ['<form>']

        for option in options:
            option_text = option['display']['definition']['value']
            # We need to replace <text> with <span> so that answer text
            # stays on the same line with checkbox/radio button
            option_text = self._replace_tag(option_text, 'text', 'span')
            result.append('<label><input type="%s" name="%s">'
                          '%s<br></label>' % (
                              input_type, question_index, option_text))

        result.append('</form>')
        return result

    def _replace_tag(self, text, initial_tag, target_tag):
        soup = BeautifulSoup(text)
        while soup.find(initial_tag):
            soup.find(initial_tag).name = target_tag
        return soup.prettify()

    def _generate_input_field(self):
        return ['<form><label>Enter answer here:<input type="text" '
                'name=""><br></label></form>']


class MarkupToHTMLConverter(object):
    def __init__(self, session):
        self._session = session

    def __call__(self, text):
        """
        Prettify instructions text to make it more suitable for offline reading.

        @param text: HTML (kinda) text to prettify.
        @type text: str

        @return: Prettified HTML with several markup tags replaced with HTML
            equivalents.
        @rtype: str
        """
        soup = BeautifulSoup(text)
        self._convert_instructions_basic(soup)
        self._convert_instructions_images(soup)
        return soup.prettify()

    def _convert_instructions_basic(self, soup):
        """
        Perform basic conversion of instructions markup. This includes
        replacement of several textual markup tags with their HTML equivalents.

        @param soup: BeautifulSoup instance.
        @type soup: BeautifulSoup
        """
        # 1. Inject basic CSS style
        css_soup = BeautifulSoup(INSTRUCTIONS_HTML_INJECTION)
        soup.append(css_soup)

        # 2. Replace <text> with <p>
        while soup.find('text'):
            soup.find('text').name = 'p'

        # 3. Replace <heading level="1"> with <h1>
        while soup.find('heading'):
            heading = soup.find('heading')
            heading.name = 'h%s' % heading.attrs.get('level', '1')

        # 4. Replace <code> with <pre>
        while soup.find('code'):
            soup.find('code').name = 'pre'

        # 5. Replace <list> with <ol> or <ul>
        while soup.find('list'):
            list_ = soup.find('list')
            type_ = list_.attrs.get('bullettype', 'numbers')
            list_.name = 'ol' if type_ == 'numbers' else 'ul'

    def _convert_instructions_images(self, soup):
        """
        Convert images of instructions markup. Images are downloaded,
        base64-encoded and inserted into <img> tags.

        @param soup: BeautifulSoup instance.
        @type soup: BeautifulSoup
        """
        # 6. Replace <img> assets with actual image contents
        images = [image for image in soup.find_all('img')
                  if image.attrs.get('assetid') is not None]
        if not images:
            return

        # Get assetid attribute from all images
        asset_ids = [image.attrs.get('assetid') for image in images]

        # Download information about image assets (image IDs)
        asset_list = get_page_json(self._session, OPENCOURSE_API_ASSETS_V1_URL,
                                   id=','.join(asset_ids))
        # Create a map "asset_id => asset" for easier access
        asset_map = dict((asset['id'], asset) for asset in asset_list['elements'])

        for image in images:
            # Download each image and encode it using base64
            url = asset_map[image['assetid']]['url']['url'].strip()
            request = self._session.get(url)
            if request.status_code == 200:
                content_type = request.headers.get('Content-Type', 'image/png')
                encoded64 = base64.b64encode(request.content).decode()
                image['src'] = 'data:%s;base64,%s' % (content_type, encoded64)


class OnDemandCourseMaterialItems(object):
    """
    Helper class that allows accessing lecture JSONs by lesson IDs.
    """
    def __init__(self, items):
        """
        Initialization. Build a map from lessonId to Lecture (item)

        @param items: linked.OnDemandCourseMaterialItems key of
            OPENCOURSE_ONDEMAND_COURSE_MATERIALS response.
        @type items: dict
        """
        # Build a map of lessonId => Item
        self._items = dict((item['lessonId'], item) for item in items)

    @staticmethod
    def create(session, course_name):
        """
        Create an instance using a session and a course_name.

        @param session: Requests session.
        @type session: requests.Session

        @param course_name: Course name (slug) from course json.
        @type course_name: str

        @return: Instance of OnDemandCourseMaterialItems
        @rtype: OnDemandCourseMaterialItems
        """

        dom = get_page_json(session, OPENCOURSE_ONDEMAND_COURSE_MATERIALS,
                            class_name=course_name)
        return OnDemandCourseMaterialItems(
            dom['linked']['onDemandCourseMaterialItems.v1'])

    def get(self, lesson_id):
        """
        Return lecture by lesson ID.

        @param lesson_id: Lesson ID.
        @type lesson_id: str

        @return: Lesson JSON.
        @rtype: dict
        Example:
        {
          "id": "AUd0k",
          "moduleId": "0MGvs",
          "lessonId": "QgCuM",
          "name": "Programming Assignment 1: Decomposition of Graphs",
          "slug": "programming-assignment-1-decomposition-of-graphs",
          "timeCommitment": 10800000,
          "content": {
            "typeName": "gradedProgramming",
            "definition": {
              "programmingAssignmentId": "zHzR5yhHEeaE0BKOcl4zJQ@2",
              "gradingWeight": 20
            }
          },
          "isLocked": true,
          "itemLockedReasonCode": "PREMIUM",
          "trackId": "core"
        },
        """
        return self._items.get(lesson_id)


class CourseraOnDemand(object):
    """
    This is a class that provides a friendly interface to extract certain
    parts of on-demand courses. On-demand class is a new format that Coursera
    is using, they contain `/learn/' in their URLs. This class does not support
    old-style Coursera classes. This API is by no means complete.
    """

    def __init__(self, session, course_id, course_name,
                 unrestricted_filenames=False):
        """
        Initialize Coursera OnDemand API.

        @param session: Current session that holds cookies and so on.
        @type session: requests.Session

        @param course_id: Course ID from course json.
        @type course_id: str

        @param unrestricted_filenames: Flag that indicates whether grabbed
            file names should endure stricter character filtering. @see
            `clean_filename` for the details.
        @type unrestricted_filenames: bool
        """
        self._session = session
        self._course_id = course_id
        self._course_name = course_name
        print('course name', course_name)

        self._unrestricted_filenames = unrestricted_filenames
        self._user_id = None

        self._quiz_to_markup = QuizExamToMarkupConverter(session)
        self._markup_to_html = MarkupToHTMLConverter(session)

    def obtain_user_id(self):
        reply = get_page_json(self._session, OPENCOURSE_MEMBERSHIPS)
        elements = reply['elements']
        user_id = elements[0]['userId'] if elements else None
        self._user_id = user_id

    def list_courses(self):
        """
        List enrolled courses.

        @return: List of enrolled courses.
        @rtype: [str]
        """
        reply = get_page_json(self._session, OPENCOURSE_MEMBERSHIPS)
        course_list = reply['linked']['courses.v1']
        slugs = [element['slug'] for element in course_list]
        return slugs

    def extract_links_from_exam(self, exam_id):
        session_id = self._get_exam_session_id(exam_id)
        exam_json = self._get_exam_json(exam_id, session_id)

        with open('quizes/exam-%s-%s.json' % (self._course_name, exam_id), 'w') as f:
            json.dump(exam_json, f)

        return self._convert_quiz_json_to_links(exam_json, 'exam')

    def extract_links_from_quiz(self, quiz_id):
        session_id = self._get_quiz_session_id(quiz_id)
        quiz_json = self._get_quiz_json(quiz_id, session_id)

        with open('quizes/quiz-%s-%s.json' % (self._class_name, quiz_id), 'w') as f:
            json.dump(quiz_json, f)

        return self._convert_quiz_json_to_links(quiz_json, 'quiz')

    def _convert_quiz_json_to_links(self, quiz_json, filename_suffix):
        markup = self._quiz_to_markup(quiz_json)
        html = self._markup_to_html(markup)

        supplement_links = {}
        instructions = (IN_MEMORY_MARKER + html, filename_suffix)
        extend_supplement_links(
            supplement_links, {IN_MEMORY_EXTENSION: [instructions]})
        return supplement_links

    def _get_exam_json(self, exam_id, session_id):
        headers = self._auth_headers_with_json()
        data = {"name": "getState", "argument": []}

        reply = post_page_json(self._session,
                               POST_OPENCOURSE_ONDEMAND_EXAM_SESSIONS_GET_STATE,
                               data=json.dumps(data),
                               headers=headers,
                               session_id=session_id)

        return reply['elements'][0]['result']

    def _get_exam_session_id(self, exam_id):
        headers = self._auth_headers_with_json()
        data = {'courseId': self._course_id, 'itemId': exam_id}

        _body, reply = post_page_and_reply(self._session,
                                           POST_OPENCOURSE_ONDEMAND_EXAM_SESSIONS,
                                           data=json.dumps(data),
                                           headers=headers)
        return reply.headers.get('X-Coursera-Id')

    def _get_quiz_json(self, quiz_id, session_id):
        headers = self._auth_headers_with_json()
        data = {"contentRequestBody": {"argument": []}}

        reply = post_page_json(self._session,
                               POST_OPENCOURSE_API_QUIZ_SESSION_GET_STATE,
                               data=json.dumps(data),
                               headers=headers,
                               user_id=self._user_id,
                               class_name=self._course_name,
                               quiz_id=quiz_id,
                               session_id=session_id)
        return reply['contentResponseBody']['return']

    def _get_quiz_session_id(self, quiz_id):
        headers = self._auth_headers_with_json()
        data = {"contentRequestBody":[]}
        reply = post_page_json(self._session,
                               POST_OPENCOURSE_API_QUIZ_SESSION,
                               data=json.dumps(data),
                               headers=headers,
                               user_id=self._user_id,
                               class_name=self._course_name,
                               quiz_id=quiz_id)

        return reply['contentResponseBody']['session']['id']

    def _auth_headers_with_json(self):
        headers = prepape_auth_headers(self._session, include_cauth=True)
        headers.update({
            'Content-Type': 'application/json; charset=UTF-8'
        })
        return headers

    def extract_links_from_lecture(self,
                                   video_id, subtitle_language='en',
                                   resolution='540p', assets=None):
        """
        Return the download URLs of on-demand course video.

        @param video_id: Video ID.
        @type video_id: str

        @param subtitle_language: Subtitle language.
        @type subtitle_language: str

        @param resolution: Preferred video resolution.
        @type resolution: str

        @param assets: List of assets that may present in the video.
        @type assets: [str]

        @return: @see CourseraOnDemand._extract_links_from_text
        """
        if assets is None:
            assets = []

        links = self._extract_videos_and_subtitles_from_lecture(
            video_id, subtitle_language, resolution)

        assets = self._normalize_assets(assets)
        extend_supplement_links(
            links, self._extract_links_from_lecture_assets(assets))

        return links

    def _normalize_assets(self, assets):
        """
        Perform asset normalization. For some reason, assets that are sometimes
        present in lectures, have "@1" at the end of their id. Such "uncut"
        asset id when fed to OPENCOURSE_ASSETS_URL results in error that says:
        "Routing error: 'get-all' not implemented". To avoid that, the last
        two characters from asset id are cut off and after that that method
        works fine. It looks like, Web UI is doing the same.

        @param assets: List of asset ids.
        @type assets: [str]

        @return: Normalized list of asset ids (without trailing "@1")
        @rtype: [str]
        """
        new_assets = []

        for asset in assets:
            # For example: giAxucdaEeWJTQ5WTi8YJQ@1
            if len(asset) == 24:
                # Turn it into: giAxucdaEeWJTQ5WTi8YJQ
                asset = asset[:-2]
            new_assets.append(asset)

        return new_assets

    def _extract_links_from_lecture_assets(self, asset_ids):
        """
        Extract links to files of the asset ids.

        @param asset_ids: List of asset ids.
        @type asset_ids: [str]

        @return: @see CourseraOnDemand._extract_links_from_text
        """
        links = {}

        def _add_asset(name, url, destination):
            filename, extension = os.path.splitext(clean_url(name))
            if extension is '':
                return

            extension = clean_filename(
                extension.lower().strip('.').strip(),
                self._unrestricted_filenames)
            basename = clean_filename(
                os.path.basename(filename),
                self._unrestricted_filenames)
            url = url.strip()

            if extension not in destination:
                destination[extension] = []
            destination[extension].append((url, basename))

        for asset_id in asset_ids:
            for asset in self._get_asset_urls(asset_id):
                _add_asset(asset['name'], asset['url'], links)

        return links

    def _get_asset_urls(self, asset_id):
        """
        Get list of asset urls and file names. This method may internally
        use _get_open_course_asset_urls to extract `asset` element types.

        @param asset_id: Asset ID.
        @type asset_id: str

        @return List of dictionaries with asset file names and urls.
        @rtype [{
            'name': '<filename.ext>'
            'url': '<url>'
        }]
        """
        dom = get_page_json(self._session, OPENCOURSE_ASSETS_URL, id=asset_id)
        logging.debug('Parsing JSON for asset_id <%s>.', asset_id)

        urls = []

        for element in dom['elements']:
            typeName = element['typeName']
            definition = element['definition']

            # Elements of `asset` types look as follows:
            #
            # {'elements': [{'definition': {'assetId': 'gtSfvscoEeW7RxKvROGwrw',
            #                               'name': 'Презентация к лекции'},
            #                'id': 'phxNlMcoEeWXCQ4nGuQJXw',
            #                'typeName': 'asset'}],
            #  'linked': None,
            #  'paging': None}
            #
            if typeName == 'asset':
                open_course_asset_id = definition['assetId']
                for asset in self._get_open_course_asset_urls(open_course_asset_id):
                    urls.append({'name': asset['name'].strip(),
                                 'url': asset['url'].strip()})

            # Elements of `url` types look as follows:
            #
            # {'elements': [{'definition': {'name': 'What motivates you.pptx',
            #                               'url': 'https://d396qusza40orc.cloudfront.net/learning/Powerpoints/2-4A_What_motivates_you.pptx'},
            #                'id': '0hixqpWJEeWQkg5xdHApow',
            #                'typeName': 'url'}],
            #  'linked': None,
            #  'paging': None}
            #
            elif typeName == 'url':
                urls.append({'name': definition['name'].strip(),
                             'url': definition['url'].strip()})

            else:
                logging.warning(
                    'Unknown asset typeName: %s\ndom: %s\n'
                    'If you think the downloader missed some '
                    'files, please report the issue here:\n'
                    'https://github.com/coursera-dl/coursera-dl/issues/new',
                    typeName, json.dumps(dom, indent=4))

        return urls

    def _get_open_course_asset_urls(self, asset_id):
        """
        Get list of asset urls and file names. This method only works
        with asset_ids extracted internally by _get_asset_urls method.

        @param asset_id: Asset ID.
        @type asset_id: str

        @return List of dictionaries with asset file names and urls.
        @rtype [{
            'name': '<filename.ext>'
            'url': '<url>'
        }]
        """
        dom = get_page_json(self._session, OPENCOURSE_API_ASSETS_V1_URL, id=asset_id)

        # Structure is as follows:
        # elements [ {
        #   name
        #   url {
        #       url
        return [{'name': element['name'].strip(),
                 'url': element['url']['url'].strip()}
                for element in dom['elements']]

    def _extract_videos_and_subtitles_from_lecture(self,
                                                   video_id,
                                                   subtitle_language='en',
                                                   resolution='540p'):

        dom = get_page_json(self._session, OPENCOURSE_VIDEO_URL, video_id=video_id)

        logging.debug('Parsing JSON for video_id <%s>.', video_id)
        video_content = {}

        # videos
        logging.debug('Gathering video URLs for video_id <%s>.', video_id)
        sources = dom['sources']
        sources.sort(key=lambda src: src['resolution'])
        sources.reverse()

        # Try to select resolution requested by the user.
        filtered_sources = [source
                            for source in sources
                            if source['resolution'] == resolution]

        if len(filtered_sources) == 0:
            # We will just use the 'vanilla' version of sources here, instead of
            # filtered_sources.
            logging.warning('Requested resolution %s not available for <%s>. '
                            'Downloading highest resolution available instead.',
                            resolution, video_id)
        else:
            logging.debug('Proceeding with download of resolution %s of <%s>.',
                          resolution, video_id)
            sources = filtered_sources

        video_url = sources[0]['formatSources']['video/mp4']
        video_content['mp4'] = video_url

        # subtitles and transcripts
        subtitle_nodes = [
            ('subtitles',    'srt', 'subtitle'),
            ('subtitlesTxt', 'txt', 'transcript'),
        ]
        for (subtitle_node, subtitle_extension, subtitle_description) in subtitle_nodes:
            logging.debug('Gathering %s URLs for video_id <%s>.', subtitle_description, video_id)
            subtitles = dom.get(subtitle_node)
            if subtitles is not None:
                if subtitle_language == 'all':
                    for current_subtitle_language in subtitles:
                        video_content[current_subtitle_language + '.' + subtitle_extension] = make_coursera_absolute_url(subtitles.get(current_subtitle_language))
                else:
                    if subtitle_language != 'en' and subtitle_language not in subtitles:
                        logging.warning("%s unavailable in '%s' language for video "
                                        "with video id: [%s], falling back to 'en' "
                                        "%s", subtitle_description.capitalize(), subtitle_language, video_id, subtitle_description)
                        subtitle_language = 'en'

                    subtitle_url = subtitles.get(subtitle_language)
                    if subtitle_url is not None:
                        # some subtitle urls are relative!
                        video_content[subtitle_language + '.' + subtitle_extension] = make_coursera_absolute_url(subtitle_url)

        lecture_video_content = {}
        for key, value in iteritems(video_content):
            lecture_video_content[key] = [(value, '')]

        return lecture_video_content

    def extract_links_from_programming(self, element_id):
        """
        Return a dictionary with links to supplement files (pdf, csv, zip,
        ipynb, html and so on) extracted from graded programming assignment.

        @param element_id: Element ID to extract files from.
        @type element_id: str

        @return: @see CourseraOnDemand._extract_links_from_text
        """
        logging.debug('Gathering supplement URLs for element_id <%s>.', element_id)

        # Assignment text (instructions) contains asset tags which describe
        # supplementary files.
        text = ''.join(self._extract_assignment_text(element_id))
        if not text:
            return {}

        supplement_links = self._extract_links_from_text(text)

        prettifier = MarkupToHTMLConverter(self._session)
        instructions = (IN_MEMORY_MARKER + prettifier.prettify(text),
                        'instructions')
        extend_supplement_links(
            supplement_links, {IN_MEMORY_EXTENSION: [instructions]})

        return supplement_links

    def extract_links_from_supplement(self, element_id):
        """
        Return a dictionary with supplement files (pdf, csv, zip, ipynb, html
        and so on) extracted from supplement page.

        @return: @see CourseraOnDemand._extract_links_from_text
        """
        logging.debug('Gathering supplement URLs for element_id <%s>.', element_id)

        dom = get_page_json(self._session, OPENCOURSE_SUPPLEMENT_URL,
                            course_id=self._course_id, element_id=element_id)

        supplement_content = {}

        # Supplement content has structure as follows:
        # 'linked' {
        #   'openCourseAssets.v1' [ {
        #       'definition' {
        #           'value'

        prettifier = MarkupToHTMLConverter(self._session)
        for asset in dom['linked']['openCourseAssets.v1']:
            value = asset['definition']['value']
            # Supplement lecture types are known to contain both <asset> tags
            # and <a href> tags (depending on the course), so we extract
            # both of them.
            extend_supplement_links(
                supplement_content, self._extract_links_from_text(value))

            instructions = (IN_MEMORY_MARKER + prettifier.prettify(value),
                            'instructions')
            extend_supplement_links(
                supplement_content, {IN_MEMORY_EXTENSION: [instructions]})

        return supplement_content

    def _extract_asset_tags(self, text):
        """
        Extract asset tags from text into a convenient form.

        @param text: Text to extract asset tags from. This text contains HTML
            code that is parsed by BeautifulSoup.
        @type text: str

        @return: Asset map.
        @rtype: {
            '<id>': {
                'name': '<name>',
                'extension': '<extension>'
            },
            ...
        }
        """
        soup = BeautifulSoup(text)
        asset_tags_map = {}

        for asset in soup.find_all('asset'):
            asset_tags_map[asset['id']] = {'name': asset['name'],
                                           'extension': asset['extension']}

        return asset_tags_map

    def _extract_asset_urls(self, asset_ids):
        """
        Extract asset URLs along with asset ids.

        @param asset_ids: List of ids to get URLs for.
        @type assertn: [str]

        @return: List of dictionaries with asset URLs and ids.
        @rtype: [{
            'id': '<id>',
            'url': '<url>'
        }]
        """
        dom = get_page_json(self._session, OPENCOURSE_ASSET_URL,
                            ids=quote_plus(','.join(asset_ids)))

        return [{'id': element['id'],
                 'url': element['url'].strip()}
                for element in dom['elements']]

    def _extract_assignment_text(self, element_id):
        """
        Extract assignment text (instructions).

        @param element_id: Element id to extract assignment instructions from.
        @type element_id: str

        @return: List of assignment text (instructions).
        @rtype: [str]
        """
        dom = get_page_json(self._session, OPENCOURSE_PROGRAMMING_ASSIGNMENTS_URL,
                            course_id=self._course_id, element_id=element_id)


        return [element['submissionLearnerSchema']['definition']
                ['assignmentInstructions']['definition']['value']
                for element in dom['elements']]

    def _extract_links_from_text(self, text):
        """
        Extract supplement links from the html text. Links may be provided
        in two ways:
            1. <a> tags with href attribute
            2. <asset> tags with id attribute (requires additional request
               to get the direct URL to the asset file)

        @param text: HTML text.
        @type text: str

        @return: Dictionary with supplement links grouped by extension.
        @rtype: {
            '<extension1>': [
                ('<link1>', '<title1>'),
                ('<link2>', '<title2')
            ],
            'extension2': [
                ('<link3>', '<title3>'),
                ('<link4>', '<title4>')
            ],
            ...
        }
        """
        supplement_links = self._extract_links_from_a_tags_in_text(text)

        extend_supplement_links(
            supplement_links,
            self._extract_links_from_asset_tags_in_text(text))

        return supplement_links

    def _extract_links_from_asset_tags_in_text(self, text):
        """
        Scan the text and extract asset tags and links to corresponding
        files.

        @param text: Page text.
        @type text: str

        @return: @see CourseraOnDemand._extract_links_from_text
        """
        # Extract asset tags from instructions text
        asset_tags_map = self._extract_asset_tags(text)
        ids = list(iterkeys(asset_tags_map))
        if not ids:
            return {}

        # asset tags contain asset names and ids. We need to make another
        # HTTP request to get asset URL.
        asset_urls = self._extract_asset_urls(ids)

        supplement_links = {}

        # Build supplement links, providing nice titles along the way
        for asset in asset_urls:
            title = clean_filename(
                asset_tags_map[asset['id']]['name'],
                self._unrestricted_filenames)
            extension = clean_filename(
                asset_tags_map[asset['id']]['extension'].strip(),
                self._unrestricted_filenames)
            url = asset['url'].strip()
            if extension not in supplement_links:
                supplement_links[extension] = []
            supplement_links[extension].append((url, title))

        return supplement_links

    def _extract_links_from_a_tags_in_text(self, text):
        """
        Extract supplement links from the html text that contains <a> tags
        with href attribute.

        @param text: HTML text.
        @type text: str

        @return: Dictionary with supplement links grouped by extension.
        @rtype: {
            '<extension1>': [
                ('<link1>', '<title1>'),
                ('<link2>', '<title2')
            ],
            'extension2': [
                ('<link3>', '<title3>'),
                ('<link4>', '<title4>')
            ]
        }
        """
        soup = BeautifulSoup(text)
        links = [item['href'].strip()
                 for item in soup.find_all('a') if 'href' in item.attrs]
        links = sorted(list(set(links)))
        supplement_links = {}

        for link in links:
            filename, extension = os.path.splitext(clean_url(link))
            # Some courses put links to sites in supplement section, e.g.:
            # http://pandas.pydata.org/
            if extension is '':
                continue

            # Make lowercase and cut the leading/trailing dot
            extension = clean_filename(
                extension.lower().strip('.').strip(),
                self._unrestricted_filenames)
            basename = clean_filename(
                os.path.basename(filename),
                self._unrestricted_filenames)
            if extension not in supplement_links:
                supplement_links[extension] = []
            # Putting basename into the second slot of the tuple is important
            # because that will allow to download many supplements within a
            # single lecture, e.g.:
            # 01_slides-presented-in-this-module.pdf
            # 01_slides-presented-in-this-module_Dalal-cvpr05.pdf
            # 01_slides-presented-in-this-module_LM-3dtexton.pdf
            supplement_links[extension].append((link, basename))

        return supplement_links
