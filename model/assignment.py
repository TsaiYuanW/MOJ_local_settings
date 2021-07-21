from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import CASCADE, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext, gettext_lazy as _
from jsonfield import JSONField
from lupa import LuaRuntime
from moss import MOSS_LANG_C, MOSS_LANG_CC, MOSS_LANG_JAVA, MOSS_LANG_PYTHON

from judge import assignment_format
from judge.models.problem import Problem
from judge.models.profile import Organization, Profile
from judge.models.submission import Submission
from judge.ratings import rate_assignment

__all__ = ['Assignment', 'AssignmentTag', 'AssignmentParticipation', 'AssignmentProblem', 'AssignmentSubmission', 'Rating']


class MinValueOrNoneValidator(MinValueValidator):
    def compare(self, a, b):
        return a is not None and b is not None and super().compare(a, b)


class AssignmentTag(models.Model):
    color_validator = RegexValidator('^#(?:[A-Fa-f0-9]{3}){1,2}$', _('Invalid colour.'))

    name = models.CharField(max_length=20, verbose_name=_('tag name'), unique=True,
                            validators=[RegexValidator(r'^[a-z-]+$', message=_('Lowercase letters and hyphens only.'))])
    color = models.CharField(max_length=7, verbose_name=_('tag colour'), validators=[color_validator])
    description = models.TextField(verbose_name=_('tag description'), blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('assignment_tag', args=[self.name])

    @property
    def text_color(self, cache={}):
        if self.color not in cache:
            if len(self.color) == 4:
                r, g, b = [ord(bytes.fromhex(i * 2)) for i in self.color[1:]]
            else:
                r, g, b = [i for i in bytes.fromhex(self.color[1:])]
            cache[self.color] = '#000' if 299 * r + 587 * g + 144 * b > 140000 else '#fff'
        return cache[self.color]

    class Meta:
        verbose_name = _('assignment tag')
        verbose_name_plural = _('assignment tags')


class Assignment(models.Model):
    SCOREBOARD_VISIBLE = 'V'
    SCOREBOARD_AFTER_CONTEST = 'C'
    SCOREBOARD_AFTER_PARTICIPATION = 'P'
    SCOREBOARD_VISIBILITY = (
        (SCOREBOARD_VISIBLE, _('Visible')),
        (SCOREBOARD_AFTER_CONTEST, _('Hidden for duration of assignment')),
        (SCOREBOARD_AFTER_PARTICIPATION, _('Hidden for duration of participation')),
    )
    key = models.CharField(max_length=20, verbose_name=_('assignment id'), unique=True,
                           validators=[RegexValidator('^[a-z0-9]+$', _('Assignment id must be ^[a-z0-9]+$'))])
    name = models.CharField(max_length=100, verbose_name=_('assignment name'), db_index=True)
    authors = models.ManyToManyField(Profile, help_text=_('These users will be able to edit the assignment.'),
                                     related_name='authors+')
    curators = models.ManyToManyField(Profile, help_text=_('These users will be able to edit the assignment, '
                                                           'but will not be listed as authors.'),
                                      related_name='curators+', blank=True)
    testers = models.ManyToManyField(Profile, help_text=_('These users will be able to view the assignment, '
                                                          'but not edit it.'),
                                     blank=True, related_name='testers+')
    description = models.TextField(verbose_name=_('description'), blank=True)
    problems = models.ManyToManyField(Problem, verbose_name=_('problems'), through='AssignmentProblem')
    start_time = models.DateTimeField(verbose_name=_('start time'), db_index=True)
    end_time = models.DateTimeField(verbose_name=_('end time'), db_index=True)
    time_limit = models.DurationField(verbose_name=_('time limit'), blank=True, null=True)
    is_visible = models.BooleanField(verbose_name=_('publicly visible'), default=False,
                                     help_text=_('Should be set even for organization-private assignments, where it '
                                                 'determines whether the assignment is visible to members of the '
                                                 'specified organizations.'))
    is_rated = models.BooleanField(verbose_name=_('assignment rated'), help_text=_('Whether this assignment can be rated.'),
                                   default=False)
    view_assignment_scoreboard = models.ManyToManyField(Profile, verbose_name=_('view assignment scoreboard'), blank=True,
                                                     related_name='view_assignment_scoreboard',
                                                     help_text=_('These users will be able to view the scoreboard.'))
    scoreboard_visibility = models.CharField(verbose_name=_('scoreboard visibility'), default=SCOREBOARD_VISIBLE,
                                             max_length=1, help_text=_('Scoreboard visibility through the duration '
                                                                       'of the assignment'), choices=SCOREBOARD_VISIBILITY)
    use_clarifications = models.BooleanField(verbose_name=_('no comments'),
                                             help_text=_("Use clarification system instead of comments."),
                                             default=True)
    rating_floor = models.IntegerField(verbose_name=('rating floor'), help_text=_('Rating floor for assignment'),
                                       null=True, blank=True)
    rating_ceiling = models.IntegerField(verbose_name=('rating ceiling'), help_text=_('Rating ceiling for assignment'),
                                         null=True, blank=True)
    rate_all = models.BooleanField(verbose_name=_('rate all'), help_text=_('Rate all users who joined.'), default=False)
    rate_exclude = models.ManyToManyField(Profile, verbose_name=_('exclude from ratings'), blank=True,
                                          related_name='rate_exclude+')
    is_private = models.BooleanField(verbose_name=_('private to specific users'), default=False)
    private_assignmentants = models.ManyToManyField(Profile, blank=True, verbose_name=_('private assignmentants'),
                                                 help_text=_('If private, only these users may see the assignment'),
                                                 related_name='private_assignmentants+')
    hide_problem_tags = models.BooleanField(verbose_name=_('hide problem tags'),
                                            help_text=_('Whether problem tags should be hidden by default.'),
                                            default=False)
    hide_problem_authors = models.BooleanField(verbose_name=_('hide problem authors'),
                                               help_text=_('Whether problem authors should be hidden by default.'),
                                               default=False)
    run_pretests_only = models.BooleanField(verbose_name=_('run pretests only'),
                                            help_text=_('Whether judges should grade pretests only, versus all '
                                                        'testcases. Commonly set during a assignment, then unset '
                                                        'prior to rejudging user submissions when the assignment ends.'),
                                            default=False)
    is_organization_private = models.BooleanField(verbose_name=_('private to organizations'), default=False)
    organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('organizations'),
                                           help_text=_('If private, only these organizations may see the assignment'))
    og_image = models.CharField(verbose_name=_('OpenGraph image'), default='', max_length=150, blank=True)
    logo_override_image = models.CharField(verbose_name=_('Logo override image'), default='', max_length=150,
                                           blank=True,
                                           help_text=_('This image will replace the default site logo for users '
                                                       'inside the assignment.'))
    tags = models.ManyToManyField(AssignmentTag, verbose_name=_('assignment tags'), blank=True, related_name='assignments')
    user_count = models.IntegerField(verbose_name=_('the amount of live participants'), default=0)
    summary = models.TextField(blank=True, verbose_name=_('assignment summary'),
                               help_text=_('Plain-text, shown in meta description tag, e.g. for social media.'))
    access_code = models.CharField(verbose_name=_('access code'), blank=True, default='', max_length=255,
                                   help_text=_('An optional code to prompt assignmentants before they are allowed '
                                               'to join the assignment. Leave it blank to disable.'))
    banned_users = models.ManyToManyField(Profile, verbose_name=_('personae non gratae'), blank=True,
                                          help_text=_('Bans the selected users from joining this assignment.'))
    format_name = models.CharField(verbose_name=_('assignment format'), default='default', max_length=32,
                                   choices=assignment_format.choices(), help_text=_('The assignment format module to use.'))
    format_config = JSONField(verbose_name=_('assignment format configuration'), null=True, blank=True,
                              help_text=_('A JSON object to serve as the configuration for the chosen assignment format '
                                          'module. Leave empty to use None. Exact format depends on the assignment format '
                                          'selected.'))
    problem_label_script = models.TextField(verbose_name='assignment problem label script', blank=True,
                                            help_text='A custom Lua function to generate problem labels. Requires a '
                                                      'single function with an integer parameter, the zero-indexed '
                                                      'assignment problem index, and returns a string, the label.')
    locked_after = models.DateTimeField(verbose_name=_('assignment lock'), null=True, blank=True,
                                        help_text=_('Prevent submissions from this assignment '
                                                    'from being rejudged after this date.'))
    points_precision = models.IntegerField(verbose_name=_('precision points'), default=3,
                                           validators=[MinValueValidator(0), MaxValueValidator(10)],
                                           help_text=_('Number of digits to round points to.'))

    @cached_property
    def format_class(self):
        return assignment_format.formats[self.format_name]

    @cached_property
    def format(self):
        return self.format_class(self, self.format_config)

    @cached_property
    def get_label_for_problem(self):
        if not self.problem_label_script:
            return self.format.get_label_for_problem

        def DENY_ALL(obj, attr_name, is_setting):
            raise AttributeError()
        lua = LuaRuntime(attribute_filter=DENY_ALL, register_eval=False, register_builtins=False)
        return lua.eval(self.problem_label_script)

    def clean(self):
        # Django will complain if you didn't fill in start_time or end_time, so we don't have to.
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError('What is this? A assignment that ended before it starts?')
        self.format_class.validate(self.format_config)

        try:
            # a assignment should have at least one problem, with assignment problem index 0
            # so test it to see if the script returns a valid label.
            label = self.get_label_for_problem(0)
        except Exception as e:
            raise ValidationError('Assignment problem label script: %s' % e)
        else:
            if not isinstance(label, str):
                raise ValidationError('Assignment problem label script: script should return a string.')

    def is_in_assignment(self, user):
        if user.is_authenticated:
            profile = user.profile
            return profile and profile.current_assignment is not None and profile.current_assignment.assignment == self
        return False

    def can_see_own_scoreboard(self, user):
        if self.can_see_full_scoreboard(user):
            return True
        if not self.can_join:
            return False
        if not self.show_scoreboard and not self.is_in_assignment(user):
            return False
        return True

    def can_see_full_scoreboard(self, user):
        if self.show_scoreboard:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.see_private_assignment') or user.has_perm('judge.edit_all_assignment'):
            return True
        if user.profile.id in self.editor_ids:
            return True
        if self.view_assignment_scoreboard.filter(id=user.profile.id).exists():
            return True
        if self.scoreboard_visibility == self.SCOREBOARD_AFTER_PARTICIPATION and self.has_completed_assignment(user):
            return True
        return False

    def has_completed_assignment(self, user):
        if user.is_authenticated:
            participation = self.users.filter(virtual=AssignmentParticipation.LIVE, user=user.profile).first()
            if participation and participation.ended:
                return True
        return False

    @cached_property
    def show_scoreboard(self):
        if not self.can_join:
            return False
        if (self.scoreboard_visibility in (self.SCOREBOARD_AFTER_CONTEST, self.SCOREBOARD_AFTER_PARTICIPATION) and
                not self.ended):
            return False
        return True

    @property
    def assignment_window_length(self):
        return self.end_time - self.start_time

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @cached_property
    def can_join(self):
        return self.start_time <= self._now

    @property
    def time_before_start(self):
        if self.start_time >= self._now:
            return self.start_time - self._now
        else:
            return None

    @property
    def time_before_end(self):
        if self.end_time >= self._now:
            return self.end_time - self._now
        else:
            return None

    @cached_property
    def ended(self):
        return self.end_time < self._now

    @cached_property
    def author_ids(self):
        return Assignment.authors.through.objects.filter(assignment=self).values_list('profile_id', flat=True)

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(
            Assignment.curators.through.objects.filter(assignment=self).values_list('profile_id', flat=True))

    @cached_property
    def tester_ids(self):
        return Assignment.testers.through.objects.filter(assignment=self).values_list('profile_id', flat=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('assignment_view', args=(self.key,))

    def update_user_count(self):
        self.user_count = self.users.filter(virtual=0).count()
        self.save()

    update_user_count.alters_data = True

    class Inaccessible(Exception):
        pass

    class PrivateAssignment(Exception):
        pass

    def access_check(self, user):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated:
            # Unauthenticated users can only see visible, non-private assignments
            if not self.is_visible:
                raise self.Inaccessible()
            if self.is_private or self.is_organization_private:
                raise self.PrivateAssignment()
            return

        # If the user can view or edit all assignments
        if user.has_perm('judge.see_private_assignment') or user.has_perm('judge.edit_all_assignment'):
            return

        # User is organizer or curator for assignment
        if user.profile.id in self.editor_ids:
            return

        # User is tester for assignment
        if user.profile.id in self.tester_ids:
            return

        # Assignment is not publicly visible
        if not self.is_visible:
            raise self.Inaccessible()

        # Assignment is not private
        if not self.is_private and not self.is_organization_private:
            return

        if self.view_assignment_scoreboard.filter(id=user.profile.id).exists():
            return

        in_org = self.organizations.filter(id__in=user.profile.organizations.all()).exists()
        in_users = self.private_assignmentants.filter(id=user.profile.id).exists()

        if not self.is_private and self.is_organization_private:
            if in_org:
                return
            raise self.PrivateAssignment()

        if self.is_private and not self.is_organization_private:
            if in_users:
                return
            raise self.PrivateAssignment()

        if self.is_private and self.is_organization_private:
            if in_org and in_users:
                return
            raise self.PrivateAssignment()

    def is_accessible_by(self, user):
        try:
            self.access_check(user)
        except (self.Inaccessible, self.PrivateAssignment):
            return False
        else:
            return True

    def is_editable_by(self, user):
        # If the user can edit all assignments
        if user.has_perm('judge.edit_all_assignment'):
            return True

        # If the user is a assignment organizer or curator
        if user.has_perm('judge.edit_own_assignment') and user.profile.id in self.editor_ids:
            return True

        return False

    @classmethod
    def get_visible_assignments(cls, user):
        if not user.is_authenticated:
            return cls.objects.filter(is_visible=True, is_organization_private=False, is_private=False) \
                              .defer('description').distinct()

        queryset = cls.objects.defer('description')
        if not (user.has_perm('judge.see_private_assignment') or user.has_perm('judge.edit_all_assignment')):
            q = Q(is_visible=True)
            q &= (
                Q(view_assignment_scoreboard=user.profile) |
                Q(is_organization_private=False, is_private=False) |
                Q(is_organization_private=False, is_private=True, private_assignmentants=user.profile) |
                Q(is_organization_private=True, is_private=False, organizations__in=user.profile.organizations.all()) |
                Q(is_organization_private=True, is_private=True, organizations__in=user.profile.organizations.all(),
                  private_assignmentants=user.profile)
            )

            q |= Q(authors=user.profile)
            q |= Q(curators=user.profile)
            q |= Q(testers=user.profile)
            queryset = queryset.filter(q)
        return queryset.distinct()

    def rate(self):
        with transaction.atomic():
            Rating.objects.filter(assignment__end_time__range=(self.end_time, self._now)).delete()
            for assignment in Assignment.objects.filter(
                is_rated=True, end_time__range=(self.end_time, self._now),
            ).order_by('end_time'):
                rate_assignment(assignment)

    class Meta:
        permissions = (
            ('see_private_assignment', _('See private assignments')),
            ('edit_own_assignment', _('Edit own assignments')),
            ('edit_all_assignment', _('Edit all assignments')),
            ('clone_assignment', _('Clone assignment')),
            ('moss_assignment', _('MOSS assignment')),
            ('assignment_rating', _('Rate assignments')),
            ('assignment_access_code', _('Assignment access codes')),
            ('create_private_assignment', _('Create private assignments')),
            ('change_assignment_visibility', _('Change assignment visibility')),
            ('assignment_problem_label', _('Edit assignment problem label script')),
            ('lock_assignment', _('Change lock status of assignment')),
        )
        verbose_name = _('assignment')
        verbose_name_plural = _('assignments')


class AssignmentParticipation(models.Model):
    LIVE = 0
    SPECTATE = -1

    assignment = models.ForeignKey(Assignment, verbose_name=_('associated assignment'), related_name='users', on_delete=CASCADE)
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='assignment_history', on_delete=CASCADE)
    real_start = models.DateTimeField(verbose_name=_('start time'), default=timezone.now, db_column='start')
    score = models.FloatField(verbose_name=_('score'), default=0, db_index=True)
    cumtime = models.PositiveIntegerField(verbose_name=_('cumulative time'), default=0)
    is_disqualified = models.BooleanField(verbose_name=_('is disqualified'), default=False,
                                          help_text=_('Whether this participation is disqualified.'))
    tiebreaker = models.FloatField(verbose_name=_('tie-breaking field'), default=0.0)
    virtual = models.IntegerField(verbose_name=_('virtual participation id'), default=LIVE,
                                  help_text=_('0 means non-virtual, otherwise the n-th virtual participation.'))
    format_data = JSONField(verbose_name=_('assignment format specific data'), null=True, blank=True)

    def recompute_results(self):
        with transaction.atomic():
            self.assignment.format.update_participation(self)
            if self.is_disqualified:
                self.score = -9999
                self.save(update_fields=['score'])
    recompute_results.alters_data = True

    def set_disqualified(self, disqualified):
        self.is_disqualified = disqualified
        self.recompute_results()
        if self.assignment.is_rated and self.assignment.ratings.exists():
            self.assignment.rate()
        if self.is_disqualified:
            if self.user.current_assignment == self:
                self.user.remove_assignment()
            self.assignment.banned_users.add(self.user)
        else:
            self.assignment.banned_users.remove(self.user)
    set_disqualified.alters_data = True

    @property
    def live(self):
        return self.virtual == self.LIVE

    @property
    def spectate(self):
        return self.virtual == self.SPECTATE

    @cached_property
    def start(self):
        assignment = self.assignment
        return assignment.start_time if assignment.time_limit is None and (self.live or self.spectate) else self.real_start

    @cached_property
    def end_time(self):
        assignment = self.assignment
        if self.spectate:
            return assignment.end_time
        if self.virtual:
            if assignment.time_limit:
                return self.real_start + assignment.time_limit
            else:
                return self.real_start + (assignment.end_time - assignment.start_time)
        return assignment.end_time if assignment.time_limit is None else \
            min(self.real_start + assignment.time_limit, assignment.end_time)

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @property
    def ended(self):
        return self.end_time is not None and self.end_time < self._now

    @property
    def time_remaining(self):
        end = self.end_time
        if end is not None and end >= self._now:
            return end - self._now

    def __str__(self):
        if self.spectate:
            return gettext('%s spectating in %s') % (self.user.username, self.assignment.name)
        if self.virtual:
            return gettext('%s in %s, v%d') % (self.user.username, self.assignment.name, self.virtual)
        return gettext('%s in %s') % (self.user.username, self.assignment.name)

    class Meta:
        verbose_name = _('assignment participation')
        verbose_name_plural = _('assignment participations')

        unique_together = ('assignment', 'user', 'virtual')


class AssignmentProblem(models.Model):
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='assignments', on_delete=CASCADE)
    assignment = models.ForeignKey(Assignment, verbose_name=_('assignment'), related_name='assignment_problems', on_delete=CASCADE)
    points = models.IntegerField(verbose_name=_('points'))
    partial = models.BooleanField(default=True, verbose_name=_('partial'))
    is_pretested = models.BooleanField(default=False, verbose_name=_('is pretested'))
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))
    output_prefix_override = models.IntegerField(verbose_name=_('output prefix length override'),
                                                 default=0, null=True, blank=True)
    max_submissions = models.IntegerField(help_text=_('Maximum number of submissions for this problem, '
                                                      'or leave blank for no limit.'),
                                          default=None, null=True, blank=True,
                                          validators=[MinValueOrNoneValidator(1, _('Why include a problem you '
                                                                                   'can\'t submit to?'))])

    class Meta:
        unique_together = ('problem', 'assignment')
        verbose_name = _('assignment problem')
        verbose_name_plural = _('assignment problems')
        ordering = ('order',)


class AssignmentSubmission(models.Model):
    submission = models.OneToOneField(Submission, verbose_name=_('submission'),
                                      related_name='assignment', on_delete=CASCADE)
    problem = models.ForeignKey(AssignmentProblem, verbose_name=_('problem'), on_delete=CASCADE,
                                related_name='submissions', related_query_name='submission')
    participation = models.ForeignKey(AssignmentParticipation, verbose_name=_('participation'), on_delete=CASCADE,
                                      related_name='submissions', related_query_name='submission')
    points = models.FloatField(default=0.0, verbose_name=_('points'))
    is_pretest = models.BooleanField(verbose_name=_('is pretested'),
                                     help_text=_('Whether this submission was ran only on pretests.'),
                                     default=False)

    class Meta:
        verbose_name = _('assignment submission')
        verbose_name_plural = _('assignment submissions')


class Rating(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='ratings', on_delete=CASCADE)
    assignment = models.ForeignKey(Assignment, verbose_name=_('assignment'), related_name='ratings', on_delete=CASCADE)
    participation = models.OneToOneField(AssignmentParticipation, verbose_name=_('participation'),
                                         related_name='rating', on_delete=CASCADE)
    rank = models.IntegerField(verbose_name=_('rank'))
    rating = models.IntegerField(verbose_name=_('rating'))
    volatility = models.IntegerField(verbose_name=_('volatility'))
    last_rated = models.DateTimeField(db_index=True, verbose_name=_('last rated'))

    class Meta:
        unique_together = ('user', 'assignment')
        verbose_name = _('assignment rating')
        verbose_name_plural = _('assignment ratings')


class AssignmentMoss(models.Model):
    LANG_MAPPING = [
        ('C', MOSS_LANG_C),
        ('C++', MOSS_LANG_CC),
        ('Java', MOSS_LANG_JAVA),
        ('Python', MOSS_LANG_PYTHON),
    ]

    assignment = models.ForeignKey(Assignment, verbose_name=_('assignment'), related_name='moss', on_delete=CASCADE)
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='moss', on_delete=CASCADE)
    language = models.CharField(max_length=10)
    submission_count = models.PositiveIntegerField(default=0)
    url = models.URLField(null=True, blank=True)

    class Meta:
        unique_together = ('assignment', 'problem', 'language')
        verbose_name = _('assignment moss result')
        verbose_name_plural = _('assignment moss results')

