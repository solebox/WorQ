import logging
import time
from collections import defaultdict
from pickle import dumps, loads
from uuid import uuid4
from weakref import ref as weakref

DEFAULT = 'default'
log = logging.getLogger(__name__)

class Broker(object):

    default_result_timeout = 60 * 60 * 24 # one day
    task_options = set(['result_timeout', 'taskset'])

    def __init__(self, message_queue, result_store):
        self.messages = message_queue
        self.results = result_store
        self.tasks = {stop_task.name: stop_task}

    def expose(self, obj):
        """Expose a TaskSpace or task callable to all queues.

        :param obj: A TaskSpace or task callable.
        """
        if isinstance(obj, TaskSpace):
            space = obj
        else:
            space = TaskSpace()
            space.task(obj)
        for name, func in space.tasks.iteritems():
            if name in self.tasks:
                raise ValueError('task %r conflicts with existing task' % name)
            self.tasks[name] = func

    def start_worker(self):
        """Start a worker

        This is normally a blocking call.
        """
        try:
            for queue, message in self.messages:
                self.invoke(queue, message)
        except StopBroker:
            log.info('broker stopped')

    def stop(self):
        """Stop a random worker.

        WARNING this is only meant for testing purposes. It will likely not do
        what you expect in an environment with more than one worker.
        """
        queue = self.messages.stop_queue
        self.enqueue(queue, 'stop', stop_task.name, (), {}, {})

    def discard_pending_tasks(self):
        """Discard pending tasks from all queues"""
        self.messages.discard_pending()

    def queue(self, namespace='', name=DEFAULT):
        return Queue(self, namespace, name=name)

    def enqueue(self, queue, task_id, task_name, args, kw, options):
        unknown_options = set(options) - self.task_options
        if unknown_options:
            raise ValueError('unrecognized task options: %s'
                % ', '.join(unknown_options))
        log.debug('enqueue %s [%s:%s]', task_name, queue, task_id)
        message = dumps((task_id, task_name, args, kw, options))
        if options.get('result_timeout') is not None:
            result = self.results.deferred_result(task_id)
        else:
            result = None
        self.messages.enqueue_task(queue, message)
        return result

    def invoke(self, queue, message):
        try:
            task_id, task_name, args, kw, options = loads(message)
        except Exception:
            log.error('cannot load task message: %s', message, exc_info=True)
            return
        log.debug('invoke %s [%s:%s]', task_name, queue, task_id)
        try:
            try:
                task = self.tasks[task_name]
            except KeyError:
                result = TaskError('no such task: %s [%s:%s]'
                    % (task_name, queue, task_id))
                log.error(result)
            else:
                result = task(*args, **kw)
        except StopBroker:
            result = TaskError('worker stopped')
            raise
        except KeyboardInterrupt:
            result = TaskError('interrupted')
            raise
        except BaseException, err:
            log.error('task failed: %s', task_name, exc_info=True)
            result = TaskError('%s: %s' % (type(err).__name__, err))
        finally:
            if 'taskset' in options:
                self.process_taskset(queue, options['taskset'], result)
            else:
                timeout = options.get('result_timeout')
                if timeout is not None:
                    message = dumps([result])
                    self.results.set_result(task_id, message, timeout)

    def process_taskset(self, queue, taskset, result):
        taskset_id, task_name, args, kw, options, num = taskset
        timeout = options.get('result_timeout', self.default_result_timeout)
        results = self.results.update(taskset_id, num, dumps(result), timeout)
        if results is not None:
            args = ([loads(r) for r in results],) + args
            self.enqueue(queue, taskset_id, task_name, args, kw, options)

    def deferred_result(self, task_id):
        return self.results.deferred_result(task_id)


class AbstractMessageQueue(object):
    """Message queue abstract base class

    :param url: URL used to identify the queue.
    :param *queues: One or more strings representing the names of queues to
        listen for during message iteration.
    """

    def __init__(self, url, queues):
        self.url = url
        self.queues = list(queues) if queues else [DEFAULT]

    @property
    def stop_queue(self):
        return self.queues[0]

    def __iter__(self):
        """Return an iterator that yields task messages.

        Task iteration normally blocks when there are no pending tasks to
        execute. Each yielded item must be a two-tuple consisting of
        (<queue name>, <task message>).
        """
        raise NotImplementedError('abstract method')

    def enqueue_task(self, queue, message):
        """Enqueue a task message onto a named task queue.

        :param queue: Queue name.
        :param message: Serialized task message.
        """
        raise NotImplementedError('abstract method')

    def discard_pending(self):
        """Discard pending tasks from all queues"""
        raise NotImplementedError('abstract method')


class AbstractResultStore(object):
    """Result store abstract base class

    :param url: URL used to identify the queue.
    """

    def __init__(self, url):
        self.url = url

    def deferred_result(self, task_id):
        """Return a DeferredResult object for the given task id"""
        return DeferredResult(self, task_id)

    def pop(self, task_id):
        """Pop and deserialize the result object for the given task id"""
        message = self.pop_result(task_id)
        if message is None:
            return None
        return loads(message)

    def set_result(self, task_id, message, timeout):
        """Persist serialized result message.

        Must be implemented by each broker implementation. Not normally called
        by user code.

        :param task_id: Unique task identifier string.
        :param message: Serialized result message.
        :param timeout: Number of seconds to persist the result before
            discarding it.
        """
        raise NotImplementedError('abstract method')

    def pop_result(self, task_id):
        """Pop serialized result message from persistent storage.

        Must be implemented by each broker implementation. Not normally called
        by user code.

        :param task_id: Unique task identifier string.
        :returns: The result message; None if not found.
        """
        raise NotImplementedError('abstract method')

    def update(self, taskset_id, num_tasks, message, timeout):
        """Update the result set for a task set, return all results if complete

        Must be implemented by each broker implementation. Not normally called
        by user code. This operation is atomic, meaning that only one caller
        will ever be returned a value other than None for a given `taskset_id`.

        :param taskset_id: (string) The taskset unique identifier.
        :param num_tasks: (int) Number of tasks in the set.
        :param message: (string) A serialized result object to add to the
            set of results.
        :param timeout: (int) Discard results after this number of seconds.
        :returns: None if the number of updates has not reached num_tasks.
            Otherwise return an unordered list of serialized result messages.
        """
        raise NotImplementedError('abstract method')


class Queue(object):
    """Queue object for invoking remote tasks

    NOTE two queue objects are considered equal if they refer to the same
    queue on the same broker (their namespaces may be different).
    """

    def __init__(self, broker, namespace='', name=DEFAULT):
        self.__broker = broker
        self.__name = name
        self.__namespace = namespace

    def __getattr__(self, name):
        if self.__namespace:
            name = '%s.%s' % (self.__namespace, name)
        return Queue(self.__broker, name, self.__name)

    def __call__(self, *args, **kw):
        return Task(self)(*args, **kw)

    def __eq__(self, other):
        if not isinstance(other, type(self)):
            return False
        return (self.__broker, self.__name) == (other.__broker, other.__name)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<Queue %s namespace=%s>' % (self.__name, self.__namespace)

    def __str__(self):
        return self.__name


class Task(object):

    def __init__(self, queue, **options):
        self.queue = queue
        self.name = queue._Queue__namespace
        self.options = options

    @property
    def broker(self):
        return self.queue._Queue__broker

    def __call__(self, *args, **kw):
        id = uuid4().hex
        return self.queue._Queue__broker.enqueue(
            self.queue._Queue__name, id, self.name, args, kw, self.options)

    def with_options(self, options):
        return Task(self.queue, **options)

    def __repr__(self):
        return '<Task %s [%s]>' % (self.name, self.queue._Queue__name)


class TaskError(Exception):

    def __eq__(self, other):
        return isinstance(other, TaskError) and str(self) == str(other)

    def __ne__(self, other):
        return not self.__eq__(other)


class DeferredResult(object):
    """Deferred result object

    Meaningful attributes:
    - value: The result value. This is set when evaluating the boolean value of
      the DeferredResult object after the task completes. The value of this
      attribute will be an instance of TaskError if the task could not be
      invoked or raised an error.
    """

    def __init__(self, store, task_id):
        self.store = store
        self.task_id = task_id
        self._completed = False

    def wait(self, timeout=None, poll_interval=1):
        """Wait for the task result.

        This method polls the result store. Use carefully. A task calling this
        method (waiting on the result of a task being executed by the
        same group of workers as the waiting task) may result in dead lock.

        :param timeout: Number of seconds to wait. Wait forever by default.
        :param poll_interval: Number of seconds to sleep between polling the
            result store. Default 1 second.
        :returns: True if the task completed successfully, otherwise False.
        :raises: TaskError if the task could not be invoked or raised an error.
        """
        if timeout is None:
            while not self:
                time.sleep(poll_interval)
        else:
            end = time.time() + timeout
            while not self and end > time.time():
                time.sleep(poll_interval)
        if self._completed and isinstance(self.value, TaskError):
            raise self.value
        return self._completed

    def __nonzero__(self):
        """Return True if the result has arrived, otherwise False."""
        if not self._completed:
            value = self.store.pop(self.task_id)
            if value is None:
                return False
            assert len(value) == 1, value
            self.value = value[0]
            self._completed = True
        return True

    def __repr__(self):
        if self:
            if isinstance(self.value, TaskError):
                value = self.value
            else:
                value = 'value=%r' % (self.value,)
        else:
            value = 'incomplete'
        return '<DeferredResult %s>' % (value,)


class TaskSet(object):
    """Execute a set of tasks in parallel, process results in a final task.

    :param result_timeout: Number of seconds to persist the final result. This
        timeout value is also used to retain intermediate task results. It
        should be longer than the longest-running task in the set. The default
        is None, which means the final result will be ignored; the default
        timeout for intermediate tasks is 24 hours in that case.

    Usage:
        >>> t = TaskSet(result_timeout=60)
        >>> for arg in [0, 1, 2, 3]:
        ...     t.add(q.plus_ten, arg)
        ...
        >>> t(q.sum)
        <DeferredResult value=46>

    TaskSet algorithm:
    - Head tasks (added with .add) are enqueued to be executed in parallel
      when the TaskSet is called with the final task.
    - Upon completion of each head task the results are checked to determine
      if all head tasks have completed. If so, pop the results from the
      persistent store and enqueue the final task (passed to .__call__).
      Otherwise set a timeout on the results so they are not persisted forever
      if the taskset fails to complete for whatever reason.
    """

    def __init__(self, **options):
        self.queue = None
        self.tasks = []
        self.options = options

    def add(*self_task_args, **kw):
        """Add a task to the set

        :params task: A task object.
        :params *args: Positional arguments to use when invoking the task.
        :params **kw: Keyword arguments to use when invoking the task.
        """
        if len(self_task_args) < 2:
            raise ValueError('expected at least two positional '
                'arguments, got %s' % len(self_task_args))
        self = self_task_args[0]
        task = self_task_args[1]
        if isinstance(task, Queue):
            task = Task(task)
        args = self_task_args[2:]
        if self.queue is None:
            self.queue = task.queue
        elif self.queue != task.queue:
            raise ValueError('cannot combine tasks from discrete queues: '
                '%s != %s' % (self.queue, task.queue))
        self.tasks.append((task, args, kw))

    def with_options(self, options):
        ts = TaskSet(**options)
        ts.queue = self.queue
        ts.tasks = list(self.tasks)
        return ts

    def __call__(*self_task_args, **kw):
        """Invoke the taskset

        :params task: The final task object, which will be invoked with a list
            of results from all other tasks in the set as its first argument.
        :params *args: Extra positional arguments to use when invoking the task.
        :params **kw: Keyword arguments to use when invoking the task.
        :returns: DeferredResult if the TaskSet was created with a
            `result_timeout`. Otherwise, None.
        """
        TaskSet.add(*self_task_args, **kw)
        self = self_task_args[0]
        task, args, kw = self.tasks.pop()
        num = len(self.tasks)
        taskset_id = uuid4().hex
        if self.options.get('result_timeout') is not None:
            result = task.broker.deferred_result(taskset_id)
        else:
            result = None
        options = {'taskset':
            (taskset_id, task.name, args, kw, self.options, num)}
        for t, a, k in self.tasks:
            t.with_options(options)(*a, **k)
        return result


class TaskSpace(object):
    """Task namespace container"""

    def __init__(self, name=''):
        self.name = name
        self.tasks = {}

    def task(self, callable, name=None):
        """Add a task to the namespace

        This can be used as a decorator::
            ts = TaskSpace(__name__)

            @ts.task
            def frob(value):
                db.update(value)

        :param callable: A callable object, usually a function or method.
        :param name: Task name. Defaults to `callable.__name__`.
        """
        if name is None:
            name = callable.__name__
        if self.name:
            name = '%s.%s' % (self.name, name)
        if name in self.tasks:
            raise ValueError('task %r conflicts with existing task' % name)
        self.tasks[name] = callable
        return callable


class StopBroker(BaseException): pass

def stop_task():
    raise StopBroker()
stop_task.name = '<stop_task>'
