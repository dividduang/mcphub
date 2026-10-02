from dataclasses import dataclass
from typing import Annotated, Callable

from pydantic import BaseModel, ConfigDict, Field

from backend.plugin.mcphub.crawlers.network import Fetcher, UpstreamError

Project = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')]
Version = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9][A-Za-z0-9.!+_-]*$')]
Username = Annotated[str, Field(min_length=1, max_length=40, pattern=r'^[A-Za-z0-9_-]+$')]
Limit = Annotated[int, Field(ge=1, le=100)]
ItemId = Annotated[int, Field(ge=1, le=2147483647)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProjectArgs(Arguments):
    project: Project


class ReleaseArgs(ProjectArgs):
    version: Version


class VersionsArgs(ProjectArgs):
    limit: Limit = 30


class ItemArgs(Arguments):
    item_id: ItemId


class UserArgs(Arguments):
    username: Username
    limit: Limit = 30


class FeedArgs(Arguments):
    limit: Limit = 30


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise UpstreamError('Upstream entity not found or invalid')
    return value


def _array(value) -> list:
    if not isinstance(value, list):
        raise UpstreamError('Upstream collection is invalid')
    return value


def _project(fetch: Fetcher, project: str, version: str | None = None) -> dict:
    suffix = f'/{version}' if version is not None else ''
    return _object(fetch.get(f'/pypi/{project}{suffix}/json'))


def pypi_project(fetch: Fetcher, project: Project) -> dict:
    """Read current PyPI package identity, summary, Python compatibility and license."""
    info = _object(_project(fetch, project)['info'])
    return {key: info.get(key) for key in ('name', 'version', 'summary', 'requires_python', 'license_expression', 'license', 'author', 'classifiers')}


def pypi_release(fetch: Fetcher, project: Project, version: Version) -> dict:
    """Inspect an exact PyPI release, yanked status and known vulnerability advisories."""
    data = _project(fetch, project, version)
    info = _object(data['info'])
    return {'name': info.get('name'), 'version': info.get('version'), 'yanked': info.get('yanked'),
            'yanked_reason': info.get('yanked_reason'), 'requires_python': info.get('requires_python'),
            'vulnerabilities': data.get('vulnerabilities', [])}


def pypi_versions(fetch: Fetcher, project: Project, limit: Limit = 30) -> dict:
    """List package versions from the official JSON Simple Index (source ordering)."""
    import re

    normalized = re.sub(r'[-_.]+', '-', project).lower()
    data = _object(fetch.get(f'/simple/{normalized}/', index=True))
    versions = _array(data.get('versions'))
    return {'name': data.get('name'), 'versions': versions[-limit:], 'total': len(versions), 'ordering': 'source'}


def pypi_dependencies(fetch: Fetcher, project: Project, version: Version) -> dict:
    """Read exact-release dependency requirement strings and optional extras without installing code."""
    info = _object(_project(fetch, project, version)['info'])
    return {'name': info.get('name'), 'version': info.get('version'), 'requires_python': info.get('requires_python'),
            'requirements': info.get('requires_dist') or [], 'extras': info.get('provides_extra') or []}


def pypi_files(fetch: Fetcher, project: Project, version: Version) -> dict:
    """Audit release distribution filenames, byte sizes and SHA256 hashes; never download executables."""
    data = _project(fetch, project, version)
    files = _array(data.get('urls'))
    return {'project': project, 'version': version, 'files': [
        {key: row.get(key) for key in ('filename', 'packagetype', 'size', 'digests', 'requires_python', 'upload_time_iso_8601', 'yanked')}
        for row in files
    ]}


def pypi_project_urls(fetch: Fetcher, project: Project) -> dict:
    """Find project-maintainer published documentation and repository links without following them."""
    info = _object(_project(fetch, project)['info'])
    return {'name': info.get('name'), 'project_urls': info.get('project_urls') or {}, 'package_url': info.get('package_url')}


def hn_item(fetch: Fetcher, item_id: ItemId) -> dict:
    """Read an official Hacker News story/comment with score and discussion references; HTML is untrusted text."""
    item = _object(fetch.get(f'/v0/item/{item_id}.json'))
    return {key: item.get(key) for key in ('id', 'type', 'by', 'time', 'title', 'text', 'url', 'score', 'parent', 'kids', 'descendants', 'deleted', 'dead')}


def hn_user(fetch: Fetcher, username: Username, limit: Limit = 30) -> dict:
    """Read a public Hacker News author's karma, profile and bounded submitted-item references."""
    user = _object(fetch.get(f'/v0/user/{username}.json'))
    return {'id': user.get('id'), 'created': user.get('created'), 'karma': user.get('karma'),
            'about': user.get('about'), 'submitted': _array(user.get('submitted', []))[:limit]}


def _feed(fetch: Fetcher, name: str, limit: int) -> dict:
    ids = _array(fetch.get(f'/v0/{name}stories.json'))
    return {'feed': name, 'item_ids': ids[:limit], 'available': len(ids)}


def hn_top(fetch: Fetcher, limit: Limit = 30) -> dict:
    """Read front-page ranked Hacker News story IDs, including front-page jobs."""
    return _feed(fetch, 'top', limit)


def hn_new(fetch: Fetcher, limit: Limit = 30) -> dict:
    """Read newest Hacker News story IDs in the official chronological feed."""
    return _feed(fetch, 'new', limit)


def hn_best(fetch: Fetcher, limit: Limit = 30) -> dict:
    """Read the official Hacker News best-story selection."""
    return _feed(fetch, 'best', limit)


def hn_ask(fetch: Fetcher, limit: Limit = 30) -> dict:
    """Read Ask HN discussion IDs, excluding the general news feed."""
    return _feed(fetch, 'ask', limit)


@dataclass(frozen=True)
class ToolSpec:
    function: Callable
    arguments: type[Arguments]
    title: str

    @property
    def name(self) -> str:
        return self.function.__name__

    def catalog(self) -> dict:
        return {'name': self.name, 'title': self.title, 'description': self.function.__doc__,
                'input_schema': self.arguments.model_json_schema()}


TOOLS = {
    'pypi': (
        ToolSpec(pypi_project, ProjectArgs, '1 · Package overview'),
        ToolSpec(pypi_release, ReleaseArgs, '2 · Exact release and advisories'),
        ToolSpec(pypi_versions, VersionsArgs, '3 · Published versions'),
        ToolSpec(pypi_dependencies, ReleaseArgs, '4 · Release dependencies'),
        ToolSpec(pypi_files, ReleaseArgs, '5 · Distribution integrity'),
        ToolSpec(pypi_project_urls, ProjectArgs, '6 · Project links'),
    ),
    'hackernews': (
        ToolSpec(hn_item, ItemArgs, '1 · Story and discussion'),
        ToolSpec(hn_user, UserArgs, '2 · Public author profile'),
        ToolSpec(hn_top, FeedArgs, '3 · Front-page ranking'),
        ToolSpec(hn_new, FeedArgs, '4 · New story feed'),
        ToolSpec(hn_best, FeedArgs, '5 · Best stories'),
        ToolSpec(hn_ask, FeedArgs, '6 · Ask HN discussions'),
    ),
}
CATALOG = [
    {'slug': 'pypi', 'name': 'PyPI package intelligence',
     'description': 'Official PyPI metadata, dependency and distribution integrity research. No code downloads.',
     'tools': [tool.catalog() for tool in TOOLS['pypi']]},
    {'slug': 'hackernews', 'name': 'Hacker News public research',
     'description': 'Official Hacker News public news, discussion and author data. Returned HTML/URLs are untrusted data.',
     'tools': [tool.catalog() for tool in TOOLS['hackernews']]},
]
