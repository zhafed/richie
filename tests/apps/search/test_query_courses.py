"""
Tests for environment ElasticSearch support
"""
import json
import random
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings

import arrow
from elasticsearch.client import IndicesClient
from elasticsearch.helpers import bulk

from richie.apps.search.filter_definitions.courses import IndexableFilterDefinition
from richie.apps.search.indexers.courses import CoursesIndexer

COURSES = [
    {"is_new": True, "categories": [1, 3, 5], "organizations": [11, 13, 15]},
    {"is_new": True, "categories": [2, 3], "organizations": [12, 13]},
    {"is_new": False, "categories": [1, 4, 5], "organizations": [11, 14, 15]},
    {"is_new": False, "categories": [2, 4], "organizations": [12, 14]},
]

COURSE_RUNS = {
    "A": {
        # A) ongoing course, next open course to end enrollment
        "start": arrow.utcnow().shift(days=-5).datetime,
        "end": arrow.utcnow().shift(days=+120).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-15).datetime,
        "enrollment_end": arrow.utcnow().shift(days=+5).datetime,
        "languages": ["fr"],
    },
    "B": {
        # B) ongoing course, can still be enrolled in for longer than A)
        "start": arrow.utcnow().shift(days=-15).datetime,
        "end": arrow.utcnow().shift(days=+105).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-30).datetime,
        "enrollment_end": arrow.utcnow().shift(days=+15).datetime,
        "languages": ["en"],
    },
    "C": {
        # C) not started yet, first upcoming course to start
        "start": arrow.utcnow().shift(days=+15).datetime,
        "end": arrow.utcnow().shift(days=+150).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-30).datetime,
        "enrollment_end": arrow.utcnow().shift(days=+30).datetime,
        "languages": ["en"],
    },
    "D": {
        # D) not started yet, will start after the other upcoming course
        "start": arrow.utcnow().shift(days=+45).datetime,
        "end": arrow.utcnow().shift(days=+120).datetime,
        "enrollment_start": arrow.utcnow().shift(days=+30).datetime,
        "enrollment_end": arrow.utcnow().shift(days=+60).datetime,
        "languages": ["fr", "de"],
    },
    "E": {
        # E) ongoing course, most recent to end enrollment
        "start": arrow.utcnow().shift(days=-90).datetime,
        "end": arrow.utcnow().shift(days=+15).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-120).datetime,
        "enrollment_end": arrow.utcnow().shift(days=-30).datetime,
        "languages": ["en"],
    },
    "F": {
        # F) ongoing course, enrollment has been over for the longest
        "start": arrow.utcnow().shift(days=-75).datetime,
        "end": arrow.utcnow().shift(days=+30).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-100).datetime,
        "enrollment_end": arrow.utcnow().shift(days=-45).datetime,
        "languages": ["fr"],
    },
    "G": {
        # G) the other already finished course; it finished more recently than H)
        "start": arrow.utcnow().shift(days=-80).datetime,
        "end": arrow.utcnow().shift(days=-15).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-100).datetime,
        "enrollment_end": arrow.utcnow().shift(days=-60).datetime,
        "languages": ["en"],
    },
    "H": {
        # H) the course that has been over for the longest
        "start": arrow.utcnow().shift(days=-120).datetime,
        "end": arrow.utcnow().shift(days=-30).datetime,
        "enrollment_start": arrow.utcnow().shift(days=-150).datetime,
        "enrollment_end": arrow.utcnow().shift(days=-90).datetime,
        "languages": ["en", "de"],
    },
}


@override_settings(  # Reduce the number of languages
    ALL_LANGUAGES_DICT={
        l: "#{:s}".format(l) for cr in COURSE_RUNS.values() for l in cr["languages"]
    }
)
@mock.patch.object(  # Avoid having to build the categories and organizations indices
    IndexableFilterDefinition,
    "get_i18n_names",
    return_value={str(id): "#{:d}".format(id) for id in range(20)},
)
@mock.patch.object(  # Avoid messing up the development Elasticsearch index
    CoursesIndexer,
    "index_name",
    new_callable=mock.PropertyMock,
    return_value="test_courses",
)
class CourseRunsCoursesQueryTestCase(TestCase):
    """
    Test edge case search queries on underlying course runs to make sure filtering and sorting
    works as we expect.
    """

    @staticmethod
    def get_expected_courses(courses_definition, course_run_ids):
        """
        Compute the expected course ids from the course run ids.
        """
        # Remove courses that don't have archived course runs
        # > [[3, ["H", "D"]], [2, ["G", "E"]]]
        filtered_courses = list(
            filter(
                lambda o: any([id in course_run_ids for id in o[1]]), courses_definition
            )
        )

        # Sort our courses according to the ranking of their open course runs:
        # > [[2, ["G", "E"]], [3, ["H", "D"]]]
        # Note that we only consider open course runs to sort our courses otherwise
        # some better course runs could make it incoherent. In our example, the "C"
        # course run, if taken into account, would have lead to the following sequence
        # which is not what we expect:
        #   [[2, ["H", "D"]], [3, ["G", "E"]]]
        sorted_courses = sorted(
            filtered_courses,
            key=lambda o: min(
                [course_run_ids.index(id) for id in o[1] if id in course_run_ids]
            ),
        )

        # Extract the expected list of courses
        # > [1, 3, 0]
        return list(list(zip(*sorted_courses))[0])

    def execute_query(self, querystring="", suite=None):
        """
        Not a test.
        This method is doing the heavy lifting for the tests in this class:
        - generate a set of courses randomly associated to our "interesting" course runs,
        - prepare the Elasticsearch index,
        - execute the query.
        """
        # Shuffle our course runs to assign them randomly to 4 courses
        # For example: ["H", "D", "C", "F", "B", "A", "G", "E"]
        suite = suite or random.sample(list(COURSE_RUNS), len(COURSE_RUNS))

        # Assume 4 courses and associate 2 course runs to each course
        # > [[3, ["H", "D"]], [0, ["C", "F"]], [1, ["B", "A"]], [2, ["G", "E"]]]
        courses_definition = [[i, suite[2 * i : 2 * i + 2]] for i in range(4)]  # noqa

        # Index these 4 courses in Elasticsearch
        indices_client = IndicesClient(client=settings.ES_CLIENT)
        # Delete any existing indexes so we get a clean slate
        indices_client.delete(index="_all")
        # Create an index we'll use to test the ES features
        indices_client.create(index="test_courses")
        # Use the default courses mapping from the Indexer
        indices_client.put_mapping(
            body=CoursesIndexer.mapping, doc_type="course", index="test_courses"
        )
        # Add the sorting script
        settings.ES_CLIENT.put_script(
            id="sort_list", body=CoursesIndexer.scripts["sort_list"]
        )
        # Actually insert our courses in the index
        now = arrow.utcnow()
        actions = [
            {
                "_id": course_id,
                "_index": "test_courses",
                "_op_type": "create",
                "_type": "course",
                # The sorting algorithm assumes that course runs are sorted by decreasing
                # end date in order to limit the number of iterations and courses with a
                # lot of archived courses.
                "absolute_url": {"en": "url"},
                "cover_image": {"en": "image"},
                "title": {"en": "title"},
                **COURSES[course_id],
                "course_runs": sorted(
                    [
                        # Each course randomly gets 2 course runs (thanks to above shuffle)
                        COURSE_RUNS[course_run_id]
                        for course_run_id in course_run_ids
                    ],
                    key=lambda o: now - o["end"],
                ),
            }
            for course_id, course_run_ids in courses_definition
        ]
        bulk(
            actions=actions,
            chunk_size=settings.ES_CHUNK_SIZE,
            client=settings.ES_CLIENT,
        )
        indices_client.refresh()

        response = self.client.get(f"/api/v1.0/courses/?{querystring:s}")
        self.assertEqual(response.status_code, 200)

        return courses_definition, json.loads(response.content)

    def test_query_courses_match_all(self, *_):
        """
        Validate the detailed format of the response to a match all query.
        We force the suite to a precise example because the facet count may vary if for example
        the two course runs in german end-up on the same course (in this case the facet count
        should be 1. See next test).
        """
        _, content = self.execute_query(suite=["A", "D", "G", "F", "B", "H", "C", "E"])
        self.assertEqual(
            content,
            {
                "meta": {"count": 4, "offset": 0, "total_count": 4},
                "objects": [
                    {
                        "id": "0",
                        "absolute_url": "url",
                        "categories": [1, 3, 5],
                        "cover_image": "image",
                        "organizations": [11, 13, 15],
                        "title": "title",
                    },
                    {
                        "id": "2",
                        "absolute_url": "url",
                        "categories": [1, 4, 5],
                        "cover_image": "image",
                        "organizations": [11, 14, 15],
                        "title": "title",
                    },
                    {
                        "id": "3",
                        "absolute_url": "url",
                        "categories": [2, 4],
                        "cover_image": "image",
                        "organizations": [12, 14],
                        "title": "title",
                    },
                    {
                        "id": "1",
                        "absolute_url": "url",
                        "categories": [2, 3],
                        "cover_image": "image",
                        "organizations": [12, 13],
                        "title": "title",
                    },
                ],
                "filters": {
                    "new": {
                        "human_name": "New courses",
                        "is_drilldown": False,
                        "name": "new",
                        "position": 0,
                        "values": [
                            {"count": 2, "human_name": "First session", "name": "new"}
                        ],
                    },
                    "availability": {
                        "human_name": "Availability",
                        "is_drilldown": False,
                        "name": "availability",
                        "position": 1,
                        "values": [
                            {
                                "count": 3,
                                "human_name": "Open for enrollment",
                                "name": "open",
                            },
                            {
                                "count": 2,
                                "human_name": "Coming soon",
                                "name": "coming_soon",
                            },
                            {"count": 4, "human_name": "On-going", "name": "ongoing"},
                            {"count": 2, "human_name": "Archived", "name": "archived"},
                        ],
                    },
                    "languages": {
                        "human_name": "Languages",
                        "is_drilldown": False,
                        "name": "languages",
                        "position": 4,
                        "values": [
                            {"count": 3, "human_name": "#en", "name": "en"},
                            {"count": 2, "human_name": "#de", "name": "de"},
                            {"count": 2, "human_name": "#fr", "name": "fr"},
                        ],
                    },
                    "categories": {
                        "human_name": "Categories",
                        "is_drilldown": False,
                        "name": "categories",
                        "position": 2,
                        "values": [
                            {"count": 2, "human_name": "#1", "name": "1"},
                            {"count": 2, "human_name": "#2", "name": "2"},
                            {"count": 2, "human_name": "#3", "name": "3"},
                            {"count": 2, "human_name": "#4", "name": "4"},
                            {"count": 2, "human_name": "#5", "name": "5"},
                        ],
                    },
                    "organizations": {
                        "human_name": "Organizations",
                        "is_drilldown": False,
                        "name": "organizations",
                        "position": 3,
                        "values": [
                            {"count": 2, "human_name": "#11", "name": "11"},
                            {"count": 2, "human_name": "#12", "name": "12"},
                            {"count": 2, "human_name": "#13", "name": "13"},
                            {"count": 2, "human_name": "#14", "name": "14"},
                            {"count": 2, "human_name": "#15", "name": "15"},
                        ],
                    },
                },
            },
        )

    def test_query_courses_match_all_grouped_course_runs(self, *_):
        """
        This test examines edge cases of the previous test which lead to different facet counts:
        - A/B and E/F course runs grouped under the same course:
          => 2 ongoing courses instead of 4
        - D/H course runs grouped under the same course:
          => 1 german course instead of 2
        """
        _, content = self.execute_query(suite=["A", "B", "G", "C", "D", "H", "F", "E"])
        self.assertEqual(
            content["filters"]["languages"]["values"],
            [
                {"count": 4, "human_name": "#en", "name": "en"},
                {"count": 3, "human_name": "#fr", "name": "fr"},
                {"count": 1, "human_name": "#de", "name": "de"},
            ],
        )
        self.assertEqual(
            content["filters"]["availability"]["values"],
            [
                {"count": 2, "human_name": "Open for enrollment", "name": "open"},
                {"count": 2, "human_name": "Coming soon", "name": "coming_soon"},
                {"count": 2, "human_name": "On-going", "name": "ongoing"},
                {"count": 2, "human_name": "Archived", "name": "archived"},
            ],
        )

    def test_query_courses_course_runs_filter_open_courses(self, *_):
        """
        Battle test filtering and sorting open courses.
        """
        courses_definition, content = self.execute_query("availability=open")
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["A", "B", "C"]),
        )

    def test_query_courses_course_runs_filter_availability_facets(self, *_):
        """
        Check that facet counts are affected on languages but not on availability
        when we filter on availability.
        We must fix the course runs suite because facet counts may vary if course
        runs with the same language (resp. availability) get grouped under the same
        course.
        """
        _, content = self.execute_query(
            "availability=open", suite=["A", "B", "G", "C", "D", "H", "F", "E"]
        )
        self.assertEqual(
            content["filters"]["languages"]["values"],
            [
                {"count": 2, "human_name": "#en", "name": "en"},
                {"count": 1, "human_name": "#fr", "name": "fr"},
            ],
        )
        self.assertEqual(
            content["filters"]["availability"]["values"],
            [
                {"count": 2, "human_name": "Open for enrollment", "name": "open"},
                {"count": 2, "human_name": "Coming soon", "name": "coming_soon"},
                {"count": 2, "human_name": "On-going", "name": "ongoing"},
                {"count": 2, "human_name": "Archived", "name": "archived"},
            ],
        )
        self.assertEqual(
            content["filters"]["categories"]["values"],
            [
                {"count": 2, "human_name": "#3", "name": "3"},
                {"count": 1, "human_name": "#1", "name": "1"},
                {"count": 1, "human_name": "#2", "name": "2"},
                {"count": 1, "human_name": "#5", "name": "5"},
            ],
        )

    def test_query_courses_course_runs_filter_ongoing_courses(self, *_):
        """
        Battle test filtering and sorting ongoing courses.
        """
        courses_definition, content = self.execute_query("availability=ongoing")
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["A", "B", "E", "F"]),
        )

    def test_query_courses_course_runs_filter_coming_soon_courses(self, *_):
        """
        Battle test filtering and sorting coming soon courses.
        """
        courses_definition, content = self.execute_query("availability=coming_soon")
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["C", "D"]),
        )

    def test_query_courses_course_runs_filter_archived_courses(self, *_):
        """
        Battle test filtering and sorting archived courses.
        """
        courses_definition, content = self.execute_query("availability=archived")
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["G", "H"]),
        )

    def test_query_courses_course_runs_filter_language(self, *_):
        """
        Battle test filtering and sorting courses in one language.
        """
        courses_definition, content = self.execute_query("languages=fr")
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["A", "D", "F"]),
        )

    def test_query_courses_course_runs_filter_language_facets(self, *_):
        """
        Check that facet counts are affected on availability but not on languages
        when we filter on languages.
        We must fix the course runs suite because facet counts may vary if course
        runs with the same language (resp. availability) get grouped under the same
        course.
        """
        _, content = self.execute_query(
            "languages=fr", suite=["A", "B", "G", "C", "D", "H", "F", "E"]
        )
        self.assertEqual(
            content["filters"]["languages"]["values"],
            [
                {"count": 4, "human_name": "#en", "name": "en"},
                {"count": 3, "human_name": "#fr", "name": "fr"},
                {"count": 1, "human_name": "#de", "name": "de"},
            ],
        )
        self.assertEqual(
            content["filters"]["availability"]["values"],
            [
                {"count": 1, "human_name": "Open for enrollment", "name": "open"},
                {"count": 1, "human_name": "Coming soon", "name": "coming_soon"},
                {"count": 2, "human_name": "On-going", "name": "ongoing"},
            ],
        )
        self.assertEqual(
            content["filters"]["categories"]["values"],
            [
                {"count": 2, "human_name": "#1", "name": "1"},
                {"count": 2, "human_name": "#4", "name": "4"},
                {"count": 2, "human_name": "#5", "name": "5"},
                {"count": 1, "human_name": "#2", "name": "2"},
                {"count": 1, "human_name": "#3", "name": "3"},
            ],
        )

    def test_query_courses_course_runs_filter_multiple_languages(self, *_):
        """
        Battle test filtering and sorting courses in several languages.
        """
        courses_definition, content = self.execute_query("languages=fr&languages=de")
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["A", "D", "F", "H"]),
        )

    def test_query_courses_course_runs_filter_composed(self, *_):
        """
        Battle test filtering and sorting courses on an availability AND a language.
        """
        courses_definition, content = self.execute_query(
            "availability=ongoing&languages=en"
        )
        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, ["B", "E"]),
        )

    def test_query_courses_course_runs_filter_composed_facets(self, *_):
        """
        Check that facet counts are affected on availability and languages as expected
        when we filter on both languages and availability.
        We must fix the course runs suite because facet counts may vary if course
        runs with the same language (resp. availability) get grouped under the same
        course.
        """
        _, content = self.execute_query(
            "availability=ongoing&languages=en",
            suite=["A", "B", "G", "C", "D", "H", "F", "E"],
        )
        self.assertEqual(
            content["filters"]["languages"]["values"],
            [
                {"count": 2, "human_name": "#en", "name": "en"},
                {"count": 2, "human_name": "#fr", "name": "fr"},
            ],
        )
        self.assertEqual(
            content["filters"]["availability"]["values"],
            [
                {"count": 2, "human_name": "Open for enrollment", "name": "open"},
                {"count": 1, "human_name": "Coming soon", "name": "coming_soon"},
                {"count": 2, "human_name": "On-going", "name": "ongoing"},
                {"count": 2, "human_name": "Archived", "name": "archived"},
            ],
        )
        # Only the B and E course runs are on-going and in English
        # So only courses 0 and 3 are selected
        self.assertEqual(
            content["filters"]["categories"]["values"],
            [
                {"count": 1, "human_name": "#1", "name": "1"},
                {"count": 1, "human_name": "#2", "name": "2"},
                {"count": 1, "human_name": "#3", "name": "3"},
                {"count": 1, "human_name": "#4", "name": "4"},
                {"count": 1, "human_name": "#5", "name": "5"},
            ],
        )

    def test_query_courses_filter_new(self, *_):
        """
        Battle test filtering new courses.
        """
        courses_definition, content = self.execute_query("new=new")
        # Keep only the courses that are new:
        courses_definition = filter(lambda c: c[0] in [0, 1], courses_definition)

        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, list(COURSE_RUNS)),
        )

    def test_query_courses_filter_organization(self, *_):
        """
        Battle test filtering by an organization.
        """
        courses_definition, content = self.execute_query("organizations=12")
        # Keep only the courses that are linked to organization 12:
        courses_definition = filter(lambda c: c[0] in [1, 3], courses_definition)

        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, list(COURSE_RUNS)),
        )

    def test_query_courses_filter_multiple_organizations(self, *_):
        """
        Battle test filtering by multiple organizations.
        """
        courses_definition, content = self.execute_query(
            "organizations=11&organizations=14"
        )
        # Keep only the courses that are linked to organizations 11 or 14:
        courses_definition = filter(lambda c: c[0] in [0, 2, 3], courses_definition)

        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, list(COURSE_RUNS)),
        )

    def test_query_courses_filter_category(self, *_):
        """
        Battle test filtering by an category.
        """
        courses_definition, content = self.execute_query("categories=2")
        # Keep only the courses that are linked to category 2:
        courses_definition = filter(lambda c: c[0] in [1, 3], courses_definition)

        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, list(COURSE_RUNS)),
        )

    def test_query_courses_filter_multiple_categories(self, *_):
        """
        Battle test filtering by multiple categories.
        """
        courses_definition, content = self.execute_query("categories=1&categories=4")
        # Keep only the courses that are linked to category 1 or 4:
        courses_definition = filter(lambda c: c[0] in [0, 2, 3], courses_definition)

        self.assertEqual(
            list([int(c["id"]) for c in content["objects"]]),
            self.get_expected_courses(courses_definition, list(COURSE_RUNS)),
        )