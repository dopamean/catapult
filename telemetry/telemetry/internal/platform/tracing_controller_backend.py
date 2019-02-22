# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import contextlib
import gc
import sys
import traceback
import uuid

from py_trace_event import trace_event
from telemetry.core import exceptions
from telemetry.internal.platform.tracing_agent import atrace_tracing_agent
from telemetry.internal.platform.tracing_agent import chrome_tracing_agent
from telemetry.internal.platform.tracing_agent import cpu_tracing_agent
from telemetry.internal.platform.tracing_agent import display_tracing_agent
from telemetry.internal.platform.tracing_agent import telemetry_tracing_agent
from telemetry.timeline import tracing_config
from tracing.trace_data import trace_data


# Note: TelemetryTracingAgent must be first so that we (1) can record debug
# trace events when the other agents start/stop, and (2) simplify the code
# below in _GetActiveTelemetryTracingAgent() to find the clock sync and
# telemetry metadata recorder (assumes it's always the first).
_TRACING_AGENT_CLASSES = (
    telemetry_tracing_agent.TelemetryTracingAgent,
    chrome_tracing_agent.ChromeTracingAgent,
    atrace_tracing_agent.AtraceTracingAgent,
    cpu_tracing_agent.CpuTracingAgent,
    display_tracing_agent.DisplayTracingAgent
)


def _GenerateClockSyncId():
  return str(uuid.uuid4())


@contextlib.contextmanager
def _DisableGarbageCollection():
  try:
    gc.disable()
    yield
  finally:
    gc.enable()


class _TraceDataDiscarder(object):
  """A do-nothing data builder that just discards trace data."""
  def AddTraceFor(self, trace_part, value):
    del trace_part  # Unused.
    del value  # Unused.


class _TracingState(object):

  def __init__(self, config, timeout):
    self._builder = trace_data.TraceDataBuilder()
    self._config = config
    self._timeout = timeout

  @property
  def builder(self):
    return self._builder

  @property
  def config(self):
    return self._config

  @property
  def timeout(self):
    return self._timeout


class TracingControllerBackend(object):
  def __init__(self, platform_backend):
    self._platform_backend = platform_backend
    self._current_state = None
    self._active_agents_instances = []
    self._is_tracing_controllable = True

  def SetTelemetryInfo(self, telemetry_info):
    agent = self._GetActiveTelemetryTracingAgent()
    if agent is not None:
      agent.SetTelemetryInfo(telemetry_info)

  def StartTracing(self, config, timeout):
    if self.is_tracing_running:
      return False

    assert isinstance(config, tracing_config.TracingConfig)
    assert len(self._active_agents_instances) == 0

    self._current_state = _TracingState(config, timeout)

    for agent_class in _TRACING_AGENT_CLASSES:
      if agent_class.IsSupported(self._platform_backend):
        agent = agent_class(self._platform_backend)
        with trace_event.trace('StartAgentTracing',
                               agent=str(agent.__class__.__name__)):
          if agent.StartAgentTracing(config, timeout):
            self._active_agents_instances.append(agent)

    return True

  def StopTracing(self):
    assert self.is_tracing_running, 'Can only stop tracing when tracing is on.'
    self._IssueClockSyncMarker()
    builder = self._current_state.builder

    raised_exception_messages = []
    for agent in reversed(self._active_agents_instances):
      try:
        with trace_event.trace('StopAgentTracing',
                               agent=str(agent.__class__.__name__)):
          agent.StopAgentTracing()
      except Exception: # pylint: disable=broad-except
        raised_exception_messages.append(
            ''.join(traceback.format_exception(*sys.exc_info())))

    for agent in self._active_agents_instances:
      try:
        with trace_event.trace('CollectAgentTraceData',
                               agent=str(agent.__class__.__name__)):
          agent.CollectAgentTraceData(builder)
      except Exception: # pylint: disable=broad-except
        raised_exception_messages.append(
            ''.join(traceback.format_exception(*sys.exc_info())))

    self._active_agents_instances = []
    self._current_state = None

    if raised_exception_messages:
      raise exceptions.TracingException(
          'Exceptions raised when trying to stop tracing:\n' +
          '\n'.join(raised_exception_messages))

    return builder.AsData()

  def FlushTracing(self, discard_current=False):
    assert self.is_tracing_running, 'Can only flush tracing when tracing is on.'
    self._IssueClockSyncMarker()

    raised_exception_messages = []

    # pylint: disable=redefined-variable-type
    # See: https://github.com/PyCQA/pylint/issues/710
    if discard_current:
      trace_builder = _TraceDataDiscarder()
    else:
      trace_builder = self._current_state.builder

    for agent in self._active_agents_instances:
      try:
        if agent.SupportsFlushingAgentTracing():
          with trace_event.trace('FlushAgentTracing',
                                 agent=str(agent.__class__.__name__)):
            agent.FlushAgentTracing(self._current_state.config,
                                    self._current_state.timeout,
                                    trace_builder)
      except Exception: # pylint: disable=broad-except
        raised_exception_messages.append(
            ''.join(traceback.format_exception(*sys.exc_info())))

    if raised_exception_messages:
      raise exceptions.TracingException(
          'Exceptions raised when trying to flush tracing:\n' +
          '\n'.join(raised_exception_messages))

  def _IssueClockSyncMarker(self):
    recorder = self._GetActiveTelemetryTracingAgent()
    if recorder is None:
      return

    with _DisableGarbageCollection():
      for agent in self._active_agents_instances:
        if agent.SupportsExplicitClockSync():
          sync_id = _GenerateClockSyncId()
          with trace_event.trace('RecordClockSyncMarker',
                                 agent=str(agent.__class__.__name__),
                                 sync_id=sync_id):
            agent.RecordClockSyncMarker(sync_id,
                                        recorder.RecordIssuerClockSyncMarker)

  def IsChromeTracingSupported(self):
    return chrome_tracing_agent.ChromeTracingAgent.IsSupported(
        self._platform_backend)

  @property
  def is_tracing_running(self):
    return self._current_state is not None

  @property
  def is_chrome_tracing_running(self):
    return self._GetActiveChromeTracingAgent() is not None

  def _GetActiveTelemetryTracingAgent(self):
    if not self._active_agents_instances:
      return None
    agent = self._active_agents_instances[0]
    if isinstance(agent, telemetry_tracing_agent.TelemetryTracingAgent):
      return agent
    return None

  def _GetActiveChromeTracingAgent(self):
    if not self.is_tracing_running:
      return None
    if not self._current_state.config.enable_chrome_trace:
      return None
    for agent in self._active_agents_instances:
      if isinstance(agent, chrome_tracing_agent.ChromeTracingAgent):
        return agent
    return None

  def GetChromeTraceConfig(self):
    agent = self._GetActiveChromeTracingAgent()
    if agent:
      return agent.trace_config
    return None

  def GetChromeTraceConfigFile(self):
    agent = self._GetActiveChromeTracingAgent()
    if agent:
      return agent.trace_config_file
    return None

  def ClearStateIfNeeded(self):
    chrome_tracing_agent.ClearStarupTracingStateIfNeeded(self._platform_backend)
