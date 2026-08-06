/**
 * Arena64 as an installable application — A64-020.9.
 *
 * Everything the running page knows about the service worker, the install
 * prompt, the display mode and the browser's connectivity hint. The worker
 * itself is not here: it is built from `apps/web/pwa/`, because it is a
 * separate script with a separate global scope and putting it in a layer
 * that `import`s React would invite exactly that mistake.
 *
 * `specs/frontend.md` §20 is the contract this implements.
 */
export {
  applyAppUpdate,
  type AppUpdateState,
  type AppUpdateStatus,
  dismissAppUpdate,
  getAppUpdateState,
  registerServiceWorker,
  SERVICE_WORKER_SCOPE,
  SERVICE_WORKER_URL,
  type ServiceWorkerEnvironment,
  subscribeToAppUpdate,
  useAppUpdate,
} from "./app-update";
export { useOnline } from "./connectivity";
export { isIosSafari, isStandaloneDisplay, useStandaloneDisplay } from "./display-mode";
export {
  dismissInstall,
  INSTALL_DISMISSED_KEY,
  type InstallOutcome,
  type InstallState,
  promptInstall,
  useInstall,
  watchInstallability,
} from "./install";
export { isPushSupported, type PushCapabilities, pushCapabilities } from "./push-support";
export {
  holdAppUpdate,
  isAppUpdateHeld,
  useAppUpdateHeld,
  useHoldAppUpdate,
} from "./update-hold";
