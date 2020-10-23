# Copyright 2018 Davide Spadini
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module includes 1 class, GitPython, representing a repository in GitPython.
"""

import logging
from pathlib import Path
from typing import List, Dict, Set, Generator

from git import Git as GGitPython, Repo, GitCommandError, Commit as GitCommit

from pydriller.domain.commit import Commit, ModificationType, Modification
from pydriller.git import Git
from pydriller.utils.common import get_files
from pydriller.utils.conf import Conf

logger = logging.getLogger(__name__)


class GitPython(Git):
    """
    Class representing a repository in GitPython. It contains most of the logic of
    PyDriller: obtaining the list of commits, checkout, reset, etc.
    """

    def __init__(self, path: str, conf=None):
        """
        Init the GitPython Repository.

        :param str path: path to the repository
        """
        super().__init__(path, conf)

    @property
    def git(self):
        """
        GitPython object GitPython.

        :return: GitPython
        """
        if self._git is None:
            self._open_git()
        return self._git

    @property
    def repo(self):
        """
        GitPython object Repo.

        :return: Repo
        """
        if self._repo is None:
            self._open_repository()
        return self._repo

    def _open_git(self):
        self._git = GGitPython(str(self.path))

    def clear(self):
        """
        According to GitPython's documentation, sometimes it leaks resources.
        This holds especially for Windows users. Hence, we need to clear the
        cache manually.
        """
        if self._git:
            self.git.clear_cache()
        if self._repo:
            self.repo.git.clear_cache()

    def _open_repository(self):
        self._repo = Repo(str(self.path))
        self._repo.config_writer().set_value("blame", "markUnblamableLines", "true").release()
        if self._conf.get("main_branch") is None:
            self._discover_main_branch(self._repo)

    def _discover_main_branch(self, repo):
        try:
            self._conf.set_value("main_branch", repo.active_branch.name)
        except TypeError:
            # The current HEAD is detached. In this case, it doesn't belong to
            # any branch, hence we return an empty string
            logger.info("HEAD is a detached symbolic reference, setting main branch to empty string")
            self._conf.set_value("main_branch", '')

    def get_head(self) -> Commit:
        """
        Get the head commit.

        :return: Commit of the head commit
        """
        head_commit = self.repo.head.commit
        return Commit(head_commit, self._conf)

    def get_list_commits(self, rev='HEAD', **kwargs) -> Generator[Commit, None, None]:
        """
        Return a generator of commits of all the commits in the repo.

        :return: Generator[Commit], the generator of all the commits in the
            repo
        """
        # If not specified otherwise, analyze the repository in reversed order
        if 'reverse' not in kwargs:
            kwargs['reverse'] = True

        for commit in self.repo.iter_commits(rev=rev, **kwargs):
            yield self.get_commit_from_gitpython(commit)

    def get_commit(self, commit_id: str) -> Commit:
        """
        Get the specified commit.

        :param str commit_id: hash of the commit to analyze
        :return: Commit
        """
        return Commit(self.repo.commit(commit_id),
                      self._conf)

    def get_commit_from_gitpython(self, commit: GitCommit) -> Commit:
        """
        Build a PyDriller commit object from a GitPython commit object.
        This is internal of PyDriller, I don't think users generally will need
        it.

        :param GitCommit commit: GitPython commit
        :return: Commit commit: PyDriller commit
        """
        return Commit(commit, self._conf)

    def checkout(self, _hash: str) -> None:
        """
        Checkout the repo at the speficied commit.
        BE CAREFUL: this will change the state of the repo, hence it should
        *not* be used with more than 1 thread.

        :param _hash: commit hash to checkout
        """
        self._delete_tmp_branch()
        self.git.checkout('-f', _hash, b='_PD')

    def _delete_tmp_branch(self) -> None:
        try:
            # we are already in _PD, so checkout the master branch before
            # deleting it
            if self.repo.active_branch.name == '_PD':
                self.git.checkout('-f', self._conf.get("main_branch"))
            self.repo.delete_head('_PD', force=True)
        except GitCommandError:
            logger.debug("Branch _PD not found")

    def files(self) -> List[str]:
        """
        Obtain the list of the files (excluding .git directory).

        :return: List[str], the list of the files
        """
        return get_files(str(self.path))

    def reset(self) -> None:
        """
        Reset the state of the repo, checking out the main branch and
        discarding
        local changes (-f option).

        """
        self.git.checkout('-f', self._conf.get("main_branch"))
        self._delete_tmp_branch()

    def total_commits(self) -> int:
        """
        Calculate total number of commits.

        :return: the total number of commits
        """
        return len(list(self.get_list_commits()))

    def get_commit_from_tag(self, tag: str) -> Commit:
        """
        Obtain the tagged commit.

        :param str tag: the tag
        :return: Commit commit: the commit the tag referred to
        """
        try:
            selected_tag = self.repo.tags[tag]
            return self.get_commit(selected_tag.commit.hexsha)
        except (IndexError, AttributeError):
            logger.debug('Tag %s not found', tag)
            raise

    def get_tagged_commits(self):
        """
        Obtain the hash of all the tagged commits.

        :return: list of tagged commits (can be empty if there are no tags)
        """
        tags = []
        for tag in self.repo.tags:
            if tag.commit:
                tags.append(tag.commit.hexsha)
        return tags

    def _get_blame(self, commit_hash: str, path: str, hashes_to_ignore_path: str = None):
        args = ['-w', commit_hash + '^']
        if hashes_to_ignore_path is not None:
            if self.git.version_info >= (2, 23):
                args += ["--ignore-revs-file", hashes_to_ignore_path]
            else:
                logger.info("'--ignore-revs-file' is only available from git v2.23")
        return self.git.blame(*args, '--', path).split('\n')

    def get_commits_modified_file(self, filepath: str) -> List[str]:
        """
        Given a filepath, returns all the commits that modified this file
        (following renames).

        :param str filepath: path to the file
        :return: the list of commits' hash
        """
        path = str(Path(filepath))

        commits = []
        try:
            commits = self.git.log("--follow", "--format=%H", path).split('\n')
        except GitCommandError:
            logger.debug("Could not find information of file %s", path)

        return commits

    def __del__(self):
        self.clear()
