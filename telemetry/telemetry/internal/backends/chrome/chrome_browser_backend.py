# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
import os
import pprint
import shlex
import sys

from telemetry.core import exceptions
from telemetry.core import util
from telemetry import decorators
from telemetry.internal.backends import browser_backend
from telemetry.internal.backends.chrome import extension_backend
from telemetry.internal.backends.chrome import tab_list_backend
from telemetry.internal.backends.chrome_inspector import devtools_client_backend
from telemetry.internal.browser import user_agent
from telemetry.internal.browser import web_contents
from telemetry.testing import options_for_unittests

import py_utils


class ChromeBrowserBackend(browser_backend.BrowserBackend):
  """An abstract class for chrome browser backends. Provides basic functionality
  once a remote-debugger port has been established."""
  # It is OK to have abstract methods. pylint: disable=abstract-method

  def __init__(self, platform_backend, supports_tab_control,
               supports_extensions, browser_options):
    super(ChromeBrowserBackend, self).__init__(
        platform_backend=platform_backend,
        supports_extensions=supports_extensions,
        browser_options=browser_options,
        tab_list_backend=tab_list_backend.TabListBackend)
    self._supports_tab_control = supports_tab_control
    self._devtools_client = None
    # TODO(crbug.com/799415): Move forwarder into DevToolsClientBackend
    self._forwarder = None

    self._output_profile_path = browser_options.output_profile_path
    self._extensions_to_load = browser_options.extensions_to_load

    if (self.browser_options.dont_override_profile and
        not options_for_unittests.AreSet()):
      sys.stderr.write('Warning: Not overriding profile. This can cause '
                       'unexpected effects due to profile-specific settings, '
                       'such as about:flags settings, cookies, and '
                       'extensions.\n')

  @property
  def devtools_client(self):
    return self._devtools_client

  @property
  @decorators.Cache
  def extension_backend(self):
    if not self.supports_extensions:
      return None
    return extension_backend.ExtensionBackendDict(self)

  def _ArgsNeedProxyServer(self, args):
    """Returns True if args for Chrome indicate the need for proxy server."""
    if '--enable-spdy-proxy-auth' in args:
      return True
    return [arg for arg in args if arg.startswith('--proxy-server=')]

  def GetBrowserStartupArgs(self):
    # TODO(crbug.com/787834): Move to the corresponding possible-browser class.
    assert not '--no-proxy-server' in self.browser_options.extra_browser_args, (
        '--no-proxy-server flag is disallowed as Chrome needs to be route to '
        'ts_proxy_server')
    args = []
    args.extend(self.browser_options.extra_browser_args)

    # Merge multiple instances of --enable-features and --disable-features since
    # Chrome ends up using whatever switch it finds last instead of merging
    # multiple instances.
    # TODO(crbug.com/799411): Remove this once the smarter ChromeArgsBuilder is
    # implemented.
    disable_features = set()
    enable_features = set()
    temp_args = []
    for arg in args:
      if arg.startswith('--disable-features='):
        disable_features.update(arg.split('=', 1)[1].split(','))
      elif arg.startswith('--enable-features='):
        enable_features.update(arg.split('=', 1)[1].split(','))
      else:
        temp_args.append(arg)

    if disable_features:
      temp_args.append(
          '--disable-features=%s' % ','.join(disable_features))
    if enable_features:
      temp_args.append(
          '--enable-features=%s' % ','.join(enable_features))
    args = temp_args

    args.append('--enable-net-benchmarking')
    args.append('--metrics-recording-only')
    args.append('--no-default-browser-check')
    args.append('--no-first-run')

    # Turn on GPU benchmarking extension for all runs. The only side effect of
    # the extension being on is that render stats are tracked. This is believed
    # to be effectively free. And, by doing so here, it avoids us having to
    # programmatically inspect a pageset's actions in order to determine if it
    # might eventually scroll.
    args.append('--enable-gpu-benchmarking')

    # Suppress all permission prompts by atomatically denying them.
    args.append('--deny-permission-prompts')

    # Override the need for a user gesture in order to play media.
    args.append('--autoplay-policy=no-user-gesture-required')

    if self.browser_options.disable_background_networking:
      args.append('--disable-background-networking')
    args.extend(self.GetReplayBrowserStartupArgs())
    args.extend(user_agent.GetChromeUserAgentArgumentFromType(
        self.browser_options.browser_user_agent_type))

    extensions = [extension.local_path
                  for extension in self._extensions_to_load]
    extension_str = ','.join(extensions)
    if len(extensions) > 0:
      args.append('--load-extension=%s' % extension_str)

    if self.browser_options.disable_component_extensions_with_background_pages:
      args.append('--disable-component-extensions-with-background-pages')

    # Disables the start page, as well as other external apps that can
    # steal focus or make measurements inconsistent.
    if self.browser_options.disable_default_apps:
      args.append('--disable-default-apps')

    # Disable the search geolocation disclosure infobar, as it is only shown a
    # small number of times to users and should not be part of perf comparisons.
    args.append('--disable-search-geolocation-disclosure')

    if (self.browser_options.logging_verbosity ==
        self.browser_options.NON_VERBOSE_LOGGING):
      args.extend(['--enable-logging', '--v=0'])
    elif (self.browser_options.logging_verbosity ==
          self.browser_options.VERBOSE_LOGGING):
      args.extend(['--enable-logging', '--v=1'])
    elif (self.browser_options.logging_verbosity ==
          self.browser_options.SUPER_VERBOSE_LOGGING):
      args.extend(['--enable-logging', '--v=2'])

    return args

  def GetBrowserStartupUrl(self):
    # TODO(crbug.com/787834): Move to the corresponding possible-browser class.
    return None

  # TODO(crbug.com/753948): remove this property once webview supports
  # --ignore-certificate-errors-spki-list.
  @property
  def is_webview(self):
    return False

  def GetReplayBrowserStartupArgs(self):
    replay_args = []
    network_backend = self.platform_backend.network_controller_backend
    if not network_backend.is_open:
      return []
    proxy_port = network_backend.forwarder.remote_port
    replay_args.append('--proxy-server=socks://localhost:%s' % proxy_port)
    if not self.is_webview:
      # Ignore certificate errors for certs that are signed with Wpr's root.
      # For more details on this flag, see crbug.com/753948.
      wpr_public_hash_file = os.path.join(util.GetCatapultDir(),
                                          'web_page_replay_go',
                                          'wpr_public_hash.txt')
      if not os.path.exists(wpr_public_hash_file):
        raise exceptions.PathMissingError('Unable to find %s' %
                                          wpr_public_hash_file)
      with open(wpr_public_hash_file) as f:
        wpr_public_hash = f.readline().strip()
        replay_args.append('--ignore-certificate-errors-spki-list=' +
                           wpr_public_hash)
    elif self.is_webview:
      # --ignore-certificate-errors-spki-list doesn't work with webview yet
      # (crbug.com/753948)
      replay_args.append('--ignore-certificate-errors')
    return replay_args

  def HasDevToolsConnection(self):
    return self._devtools_client and self._devtools_client.IsAlive()

  def _FindDevToolsPortAndTarget(self):
    """Clients should return a (devtools_port, browser_target) pair.

    May also raise EnvironmentError (IOError or OSError) if this information
    could not be determined; the call will be retried until it succeeds or
    a timeout is met.
    """
    raise NotImplementedError

  def _GetDevToolsClient(self):
    # If the agent does not appear to be ready, it could be because we got the
    # details of an older agent that no longer exists. It's thus important to
    # re-read and update the port and target on each retry.
    try:
      devtools_port, browser_target = self._FindDevToolsPortAndTarget()
    except EnvironmentError:
      return None  # Port information not ready, will retry.

    # Since the method may be called multiple times due to retries, we need to
    # restart the forwarder if the ports changed.
    if (self._forwarder is not None and
        self._forwarder.remote_port != devtools_port):
      self._forwarder.Close()
      self._forwarder = None

    if self._forwarder is None:
      # When running on a local platform this creates a DoNothingForwarder,
      # and by setting local_port=None we let the forwarder choose a port.
      self._forwarder = self.platform_backend.forwarder_factory.Create(
          local_port=None, remote_port=devtools_port, reverse=True)

    devtools_config = devtools_client_backend.DevToolsClientConfig(
        local_port=self._forwarder.local_port,
        remote_port=self._forwarder.remote_port,
        browser_target=browser_target,
        app_backend=self)

    logging.info('Got devtools config: %s', devtools_config)
    if not devtools_config.IsAgentReady():
      return None  # Will retry.

    return devtools_config.Create()

  def BindDevToolsClient(self):
    """Find an existing DevTools agent and bind this browser backend to it."""
    if self._devtools_client:
      # In case we are launching a second browser instance (as is done by
      # the CrOS backend), ensure that the old devtools_client is closed,
      # otherwise re-creating it will fail.
      self._devtools_client.Close()
      self._devtools_client = None

    try:
      self._devtools_client = py_utils.WaitFor(
          self._GetDevToolsClient,
          timeout=self.browser_options.browser_startup_timeout)
    except (py_utils.TimeoutException, exceptions.ProcessGoneException) as e:
      if not self.IsBrowserRunning():
        raise exceptions.BrowserGoneException(self.browser, e)
      raise exceptions.BrowserConnectionGoneException(self.browser, e)

  def _WaitForExtensionsToLoad(self):
    """ Wait for all extensions to load.
    Be sure to check whether the browser_backend supports_extensions before
    calling this method.
    """
    assert self._supports_extensions
    assert self._devtools_client, (
        'Waiting for extensions required devtool client to be initiated first')
    try:
      py_utils.WaitFor(self._AllExtensionsLoaded, timeout=60)
    except py_utils.TimeoutException:
      logging.error('ExtensionsToLoad: ' + repr(
          [e.extension_id for e in self._extensions_to_load]))
      logging.error('Extension list: ' + pprint.pformat(
          self.extension_backend, indent=4))
      raise

  def _AllExtensionsLoaded(self):
    # Extension pages are loaded from an about:blank page,
    # so we need to check that the document URL is the extension
    # page in addition to the ready state.
    for e in self._extensions_to_load:
      try:
        extension_objects = self.extension_backend[e.extension_id]
      except KeyError:
        return False
      for extension_object in extension_objects:
        try:
          res = extension_object.EvaluateJavaScript(
              """
              document.URL.lastIndexOf({{ url }}, 0) == 0 &&
              (document.readyState == 'complete' ||
               document.readyState == 'interactive')
              """,
              url='chrome-extension://%s/' % e.extension_id)
        except exceptions.EvaluateException:
          # If the inspected page is not ready, we will get an error
          # when we evaluate a JS expression, but we can just keep polling
          # until the page is ready (crbug.com/251913).
          res = None

        # TODO(tengs): We don't have full support for getting the Chrome
        # version before launch, so for now we use a generic workaround to
        # check for an extension binding bug in old versions of Chrome.
        # See crbug.com/263162 for details.
        if res and extension_object.EvaluateJavaScript(
            'chrome.runtime == null'):
          extension_object.Reload()
        if not res:
          return False
    return True

  @property
  def browser_directory(self):
    raise NotImplementedError()

  @property
  def profile_directory(self):
    raise NotImplementedError()

  @property
  def supports_tab_control(self):
    return self._supports_tab_control

  @property
  def supports_tracing(self):
    return True

  def StartTracing(self, trace_options,
                   timeout=web_contents.DEFAULT_WEB_CONTENTS_TIMEOUT):
    """
    Args:
        trace_options: An tracing_options.TracingOptions instance.
    """
    return self.devtools_client.StartChromeTracing(trace_options, timeout)

  def StopTracing(self):
    self.devtools_client.StopChromeTracing()

  def CollectTracingData(self, trace_data_builder):
    self.devtools_client.CollectChromeTracingData(trace_data_builder)

  def GetProcessName(self, cmd_line):
    """Returns a user-friendly name for the process of the given |cmd_line|."""
    if not cmd_line:
      # TODO(tonyg): Eventually we should make all of these known and add an
      # assertion.
      return 'unknown'
    if 'nacl_helper_bootstrap' in cmd_line:
      return 'nacl_helper_bootstrap'
    if ':sandboxed_process' in cmd_line:
      return 'renderer'
    if ':privileged_process' in cmd_line:
      return 'gpu-process'
    args = shlex.split(cmd_line)
    types = [arg.split('=')[1] for arg in args if arg.startswith('--type=')]
    if not types:
      return 'browser'
    return types[0]

  def Close(self):
    if self._devtools_client:
      self._devtools_client.Close()
      self._devtools_client = None
    if self._forwarder:
      self._forwarder.Close()
      self._forwarder = None

  @property
  def supports_system_info(self):
    return self.GetSystemInfo() != None

  def GetSystemInfo(self):
    return self.devtools_client.GetSystemInfo()

  @property
  def supports_memory_dumping(self):
    return True

  def DumpMemory(self, timeout=None):
    return self.devtools_client.DumpMemory(timeout=timeout)

  @property
  def supports_overriding_memory_pressure_notifications(self):
    return True

  def SetMemoryPressureNotificationsSuppressed(
      self, suppressed, timeout=web_contents.DEFAULT_WEB_CONTENTS_TIMEOUT):
    self.devtools_client.SetMemoryPressureNotificationsSuppressed(
        suppressed, timeout)

  def SimulateMemoryPressureNotification(
      self, pressure_level, timeout=web_contents.DEFAULT_WEB_CONTENTS_TIMEOUT):
    self.devtools_client.SimulateMemoryPressureNotification(
        pressure_level, timeout)

  # TODO: consider migrating profile_directory & browser_directory out of
  # browser_backend so we don't have to rely on creating browser_backend
  # before clearing browser caches.
  def ClearCaches(self):
    """ Clear system caches related to browser.

    This clears DNS caches, then clears system caches on file paths that are
    related to the browser (if
    browser_options.clear_sytem_cache_for_browser_and_profile_on_start is True).

    Note: this is done with best effort and may have no actual effects on the
    system.
    """
    platform = self.platform_backend.platform
    platform.FlushDnsCache()
    if self.browser_options.clear_sytem_cache_for_browser_and_profile_on_start:
      if platform.CanFlushIndividualFilesFromSystemCache():
        platform.FlushSystemCacheForDirectory(
            self.profile_directory)
        platform.FlushSystemCacheForDirectory(
            self.browser_directory)
      elif platform.SupportFlushEntireSystemCache():
        platform.FlushEntireSystemCache()
      else:
        logging.warning(
            'Flush system cache is not supported. Did not flush system cache.')

  @property
  def supports_cpu_metrics(self):
    return True

  @property
  def supports_memory_metrics(self):
    return True

  @property
  def supports_power_metrics(self):
    return True
