#!/usr/bin/env swift

import Foundation
import Speech
import AVFoundation
import CoreAudio
import os.log

final class SpeechHelper: NSObject {
    // private var audioDiagnosticTimer: DispatchSourceTimer?
    private var audioConfigurationObserver: NSObjectProtocol?

    // private let audioDiagnosticLock = NSLock()
    // private var audioBufferCount: UInt64 = 0
    // private var lastAudioBufferAt: TimeInterval = 0


    private var audioRestartScheduled = false
    private var audioRestartAttempts = 0

    private let maxAudioRestartAttempts = 3


    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var speechRecognizer: SFSpeechRecognizer?

    private let localeIdentifier: String
    private let stateLock = NSLock()
    private var isStopping = false

    // CoreAudio / Speech teardown state
    private var inputNode: AVAudioInputNode?
    private var tapInstalled = false

    // Prevent duplicate cleanup timers and duplicate RunLoop stops.
    private var cleanupFallbackScheduled = false
    private var finalExitStarted = false
    private var delayedExitScheduled = false
    private var hardExitScheduled = false

    // Whether the current stop request should terminate the helper.
    private var shouldExitAfterStop = false

    // Main-thread confined. Do not read or write this from Speech/audio callbacks.
    private(set) var terminationRequested = false

    private static let debugEnabled =
        ProcessInfo.processInfo.environment["SPEECH_HELPER_DEBUG"] == "1"

    private static let logFormatter = ISO8601DateFormatter()

    // 初始化系统日志
    private let logger = OSLog(subsystem: "com.local.speechhelper", category: "Debug")


    init(localeIdentifier: String) {
        self.localeIdentifier = localeIdentifier
        super.init()

        self.debugLog("初始化 SpeechHelper, 语言: \(localeIdentifier)")

        if localeIdentifier.lowercased() == "auto" || localeIdentifier.isEmpty {
            self.speechRecognizer = SFSpeechRecognizer()
        } else {
            self.speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: localeIdentifier))
        }

        if self.speechRecognizer == nil {
            self.debugLog("警告: SFSpeechRecognizer 初始化返回 nil (可能是语言代码错误或系统不支持)")
        }
    }


    private func lifecycleLog(_ message: String) {
        fputs("[SpeechHelper lifecycle] \(message)\n", stderr)
        fflush(stderr)
    }


    private func shutdownState() -> (
        isStopping: Bool,
        shouldExitAfterStop: Bool
    ) {
        stateLock.lock()
        defer { stateLock.unlock() }

        return (
            isStopping: isStopping,
            shouldExitAfterStop: shouldExitAfterStop
        )
    }


    private func requestForAudioAppend()
        -> SFSpeechAudioBufferRecognitionRequest? {

        stateLock.lock()
        defer { stateLock.unlock() }

        if isStopping {
            return nil
        }

        return recognitionRequest
    }


    // 专门用于 Debug，输出到 stderr 并写入 macOS 系统日志
    private func debugLog(_ message: String) {
        guard Self.debugEnabled else { return }

        let timestamp = Self.logFormatter.string(from: Date())
        let logMessage = "[\(timestamp)] [DEBUG] \(message)\n"

        // 输出到终端的 stderr
        fputs(logMessage, stderr)
        fflush(stderr)

        // 输出到 macOS Console.app
        //os_log("%{public}s", log: self.logger, type: .debug, message)
    }

    private func emit(_ object: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: object, options: []),
           let line = String(data: data, encoding: .utf8) {
            print(line)
            fflush(stdout)
        }
    }


    private func requestTermination() {
        precondition(Thread.isMainThread)

        terminationRequested = true
        CFRunLoopStop(CFRunLoopGetMain())
    }


    private func failAndTerminate(
        message: String,
        code: Int
    ) {
        if !Thread.isMainThread {
            DispatchQueue.main.async {
                [weak self] in

                self?.failAndTerminate(
                    message: message,
                    code: code
                )
            }

            return
        }

        lifecycleLog(
            "fatal startup error: " +
            "code=\(code), message=\(message)"
        )

        emit([
            "type": "error",
            "error": message,
            "code": code,
        ])

        stateLock.lock()

        isStopping = true
        shouldExitAfterStop = true

        let request = recognitionRequest
        recognitionRequest = nil

        stateLock.unlock()

        if audioEngine.isRunning {
            audioEngine.stop()
        }

        if tapInstalled {
            let node = (
                inputNode
                ?? audioEngine.inputNode
            )

            node.removeTap(
                onBus: 0
            )

            tapInstalled = false
        }

        audioEngine.reset()
        inputNode = nil

        request?.endAudio()

        // Dispatch cancel to background to avoid blocking main thread
        let task = recognitionTask
        DispatchQueue.global(qos: .userInitiated).async {
            task?.cancel()
        }

        requestTermination()
    }


    func requestPermissionsAndStart() {
        self.debugLog("检查语音识别权限...")

        let currentStatus = SFSpeechRecognizer.authorizationStatus()

        switch currentStatus {
        case .authorized:
            self.debugLog("语音识别权限已授权，跳过重复请求")
            requestMicrophonePermissionAndStart()

        case .notDetermined:
            SFSpeechRecognizer.requestAuthorization { [weak self] status in
                DispatchQueue.main.async {
                    guard let self = self else { return }

                    switch status {
                    case .authorized:
                        self.debugLog("权限: 已授权")
                        self.requestMicrophonePermissionAndStart()

                    case .denied:
                        self.failAndTerminate(
                            message: "Speech recognition permission denied",
                            code: 2
                        )

                    case .restricted:
                        self.failAndTerminate(
                            message: "Speech recognition restricted",
                            code: 3
                        )

                    case .notDetermined:
                        self.failAndTerminate(
                            message: "Speech recognition permission not determined",
                            code: 4
                        )

                    @unknown default:
                        self.failAndTerminate(
                            message: "Unknown speech authorization status",
                            code: 5
                        )
                    }
                }
            }

        case .denied:
            failAndTerminate(
                message: "Speech recognition permission denied",
                code: 2
            )

        case .restricted:
            failAndTerminate(
                message: "Speech recognition restricted",
                code: 3
            )

        @unknown default:
            failAndTerminate(
                message: "Unknown speech authorization status",
                code: 5
            )
        }
    }


    /*
     Speech recognition permission and microphone permission are separate TCC
     permissions on macOS.  AVAudioEngine may fail immediately on the first
     run when microphone access has not been requested explicitly.  Asking for
     it before creating the input tap makes that case deterministic and gives
     Python a useful error instead of an apparent recording flash/crash.
     */
    private func requestMicrophonePermissionAndStart() {
        precondition(Thread.isMainThread)

        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            start()

        case .notDetermined:
            self.debugLog("请求麦克风权限...")
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                DispatchQueue.main.async {
                    guard let self = self else { return }

                    if granted {
                        self.start()
                    } else {
                        self.failAndTerminate(
                            message: "Microphone permission denied",
                            code: 8
                        )
                    }
                }
            }

        case .denied, .restricted:
            failAndTerminate(
                message: "Microphone permission denied or restricted",
                code: 8
            )

        @unknown default:
            failAndTerminate(
                message: "Unknown microphone authorization status",
                code: 8
            )
        }
    }

    func handleTerminationSignal(_ signalNumber: Int32) {
        switch signalNumber {
        case SIGUSR1:
            lifecycleLog("Received SIGUSR1: graceful dictation finish")

            stop(
                exitAfterStop: true,
                cancelRecognition: false
            )

        case SIGINT:
            lifecycleLog("Received SIGINT: cancel dictation")

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            // Hard exit safety net: if Speech framework cleanup blocks the
            // main thread for more than 4 seconds, force exit the process.
            scheduleHardExitFallback(timeout: 4.0, reason: "SIGINT")

        case SIGTERM:
            lifecycleLog("Received SIGTERM: cancel dictation")

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            // Hard exit safety net for SIGTERM
            scheduleHardExitFallback(timeout: 4.0, reason: "SIGTERM")

        default:
            lifecycleLog("Received signal \(signalNumber)")

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            scheduleHardExitFallback(timeout: 4.0, reason: "signal \(signalNumber)")
        }
    }


    // private func startAudioDiagnostics() {
    //     precondition(Thread.isMainThread)

    //     audioDiagnosticTimer?.cancel()
    //     audioDiagnosticTimer = nil

    //     audioDiagnosticLock.lock()
    //     audioBufferCount = 0
    //     lastAudioBufferAt = 0
    //     audioDiagnosticLock.unlock()

    //     let timer = DispatchSource.makeTimerSource(queue: .main)

    //     timer.schedule(
    //         deadline: .now() + 0.5,
    //         repeating: 0.5
    //     )

    //     timer.setEventHandler { [weak self] in
    //         guard let self = self else { return }

    //         self.audioDiagnosticLock.lock()
    //         let count = self.audioBufferCount
    //         let lastAt = self.lastAudioBufferAt
    //         self.audioDiagnosticLock.unlock()

    //         let age: String

    //         if lastAt > 0 {
    //             age = String(
    //                 format: "%.2f",
    //                 ProcessInfo.processInfo.systemUptime - lastAt
    //             )
    //         } else {
    //             age = "never"
    //         }

    //         let state = self.shutdownState()

    //         self.lifecycleLog(
    //             "diagnostic: " +
    //             "engineRunning=\(self.audioEngine.isRunning), " +
    //             "tapInstalled=\(self.tapInstalled), " +
    //             "buffers=\(count), " +
    //             "lastBufferAge=\(age)s, " +
    //             "stopping=\(state.isStopping)"
    //         )
    //     }

    //     audioDiagnosticTimer = timer
    //     timer.resume()
    // }


    private func startAudioConfigurationObserver() {
        precondition(Thread.isMainThread)

        if let observer = audioConfigurationObserver {
            NotificationCenter.default.removeObserver(observer)
            audioConfigurationObserver = nil
        }

        audioConfigurationObserver =
            NotificationCenter.default.addObserver(
                forName:
                    .AVAudioEngineConfigurationChange,
                object: audioEngine,
                queue: .main
            ) { [weak self] _ in
                guard let self = self else {
                    return
                }

                let state = self.shutdownState()

                guard !state.isStopping else {
                    self.lifecycleLog(
                        "ignoring audio configuration " +
                        "change during shutdown"
                    )

                    return
                }

                do {
                    let deviceID = (
                        try self
                            .validateDefaultInputHardware()
                    )

                    let node = (
                        self.audioEngine.inputNode
                    )

                    let inputFormat = (
                        node.inputFormat(
                            forBus: 0
                        )
                    )

                    let outputFormat = (
                        node.outputFormat(
                            forBus: 0
                        )
                    )

                    self.lifecycleLog(
                        "AVAudioEngineConfigurationChange: " +
                        "deviceID=\(deviceID), " +
                        "engineRunning=" +
                        "\(self.audioEngine.isRunning), " +
                        "tapInstalled=" +
                        "\(self.tapInstalled), " +
                        "inputSampleRate=" +
                        "\(inputFormat.sampleRate), " +
                        "inputChannels=" +
                        "\(inputFormat.channelCount), " +
                        "outputSampleRate=" +
                        "\(outputFormat.sampleRate), " +
                        "outputChannels=" +
                        "\(outputFormat.channelCount)"
                    )

                    if !self.audioEngine.isRunning {
                        self
                            .scheduleAudioRestartAfterConfigurationChange()
                    }

                } catch {
                    self.lifecycleLog(
                        "audio input unavailable after " +
                        "configuration change: " +
                        error.localizedDescription
                    )

                    self
                        .scheduleAudioRestartAfterConfigurationChange()
                }
            }
    }


    /// Schedule a hard `_exit()` fallback that runs on a background thread.
    /// If the main thread is blocked in Speech framework cleanup (e.g.
    /// `recognitionTask?.cancel()`), the RunLoop-based cleanup timers will
    /// never fire. This ensures the process exits within `timeout` seconds.
    private func scheduleHardExitFallback(timeout: Double, reason: String) {
        guard !hardExitScheduled else { return }
        hardExitScheduled = true

        lifecycleLog(
            "Hard exit fallback scheduled: timeout=\(timeout)s, reason=\(reason)"
        )

        DispatchQueue.global(qos: .background).asyncAfter(
            deadline: .now() + timeout
        ) { [weak self] in
            guard let self = self else { return }

            // Check if the main RunLoop already exited normally.
            if self.terminationRequested && self.finalExitStarted {
                return
            }

            fputs(
                "[SpeechHelper lifecycle] Hard exit fallback triggered: " +
                "reason=\(reason), calling _exit(1)\n",
                stderr
            )
            fflush(stderr)

            // _exit() bypasses atexit handlers and Swift deinit.
            // This is a last resort to avoid being stuck forever.
            _exit(1)
        }
    }


    private func scheduleDelayedCleanupAndExit(reason: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.scheduleDelayedCleanupAndExit(reason: reason)
            }
            return
        }

        guard !finalExitStarted else { return }
        guard !delayedExitScheduled else { return }

        delayedExitScheduled = true

        lifecycleLog(
            "Speech task finished; delaying final process exit: \(reason)"
        )

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self = self else { return }

            self.completeCleanupAndExit(
                reason: "\(reason), after teardown grace period"
            )
        }
    }


    private func currentDefaultInputDeviceID()
        throws -> AudioDeviceID
    {
        precondition(Thread.isMainThread)

        var address = AudioObjectPropertyAddress(
            mSelector:
                kAudioHardwarePropertyDefaultInputDevice,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

        var deviceID = AudioDeviceID(
            kAudioObjectUnknown
        )

        var dataSize = UInt32(
            MemoryLayout<AudioDeviceID>.size
        )

        let status = AudioObjectGetPropertyData(
            AudioObjectID(
                kAudioObjectSystemObject
            ),
            &address,
            0,
            nil,
            &dataSize,
            &deviceID
        )

        guard status == noErr else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: Int(status),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Failed to obtain the default " +
                        "audio input device: OSStatus=\(status)"
                ]
            )
        }

        guard deviceID != kAudioObjectUnknown else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: 11,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "No default microphone input device is available"
                ]
            )
        }

        return deviceID
    }


    private func checkInputDeviceIsAlive(
        _ deviceID: AudioDeviceID
    ) throws {
        precondition(Thread.isMainThread)

        var address = AudioObjectPropertyAddress(
            mSelector:
                kAudioDevicePropertyDeviceIsAlive,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

        var isAlive: UInt32 = 0

        var dataSize = UInt32(
            MemoryLayout<UInt32>.size
        )

        let status = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &dataSize,
            &isAlive
        )

        guard status == noErr else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: Int(status),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Failed to query microphone state: " +
                        "OSStatus=\(status)"
                ]
            )
        }

        guard isAlive != 0 else {
            throw NSError(
                domain: "SpeechHelper.CoreAudio",
                code: 12,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "The default microphone input device is not active"
                ]
            )
        }
    }


    private func validateDefaultInputHardware()
        throws -> AudioDeviceID
    {
        precondition(Thread.isMainThread)

        let deviceID = try currentDefaultInputDeviceID()

        try checkInputDeviceIsAlive(
            deviceID
        )

        lifecycleLog(
            "default input device validated: " +
            "deviceID=\(deviceID)"
        )

        return deviceID
    }


    private func installInputTap() throws {
        precondition(Thread.isMainThread)

        /*
        Validate the real CoreAudio default input device first.

        AVAudioEngine.inputNode can sometimes return a format object
        even when there is no currently usable hardware input route.
        In that state, installTap() may raise an Objective-C NSException.
        */
        let deviceID = try validateDefaultInputHardware()

        let node = audioEngine.inputNode
        inputNode = node

        if tapInstalled {
            node.removeTap(
                onBus: 0
            )

            tapInstalled = false
        }

        /*
        Log both formats.

        inputFormat and outputFormat can differ during Bluetooth
        profile changes or CoreAudio route transitions.
        */
        let inputFormat = node.inputFormat(
            forBus: 0
        )

        let outputFormat = node.outputFormat(
            forBus: 0
        )

        lifecycleLog(
            "installing input tap: " +
            "deviceID=\(deviceID), " +
            "inputSampleRate=\(inputFormat.sampleRate), " +
            "inputChannels=\(inputFormat.channelCount), " +
            "outputSampleRate=\(outputFormat.sampleRate), " +
            "outputChannels=\(outputFormat.channelCount)"
        )

        guard inputFormat.sampleRate.isFinite,
            inputFormat.sampleRate > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Invalid microphone input sample rate: " +
                        "\(inputFormat.sampleRate)"
                ]
            )
        }

        guard inputFormat.channelCount > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "The microphone has no available input channels"
                ]
            )
        }

        guard outputFormat.sampleRate.isFinite,
            outputFormat.sampleRate > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Invalid microphone output sample rate: " +
                        "\(outputFormat.sampleRate)"
                ]
            )
        }

        guard outputFormat.channelCount > 0 else {
            throw NSError(
                domain: "SpeechHelper",
                code: 10,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "The microphone input node has no output channels"
                ]
            )
        }

        /*
        Important:

        Do not force outputFormat here.

        Passing a previously queried AVAudioFormat can fail when
        CoreAudio is switching devices or Bluetooth profiles.

        format: nil tells AVAudioEngine to use the input node's current
        native output format.
        */
        node.installTap(
            onBus: 0,
            bufferSize: 512,
            format: nil
        ) { [weak self] buffer, _ in
            guard let self = self else {
                return
            }

            let request = (
                self.requestForAudioAppend()
            )

            request?.append(
                buffer
            )
        }

        tapInstalled = true

        lifecycleLog(
            "input tap installed successfully"
        )
    }


    private func scheduleAudioRestartAfterConfigurationChange() {
        precondition(Thread.isMainThread)

        let state = shutdownState()

        guard !state.isStopping else {
            lifecycleLog(
                "ignoring audio configuration change during shutdown"
            )
            return
        }

        guard !audioRestartScheduled else {
            lifecycleLog("audio restart already scheduled")
            return
        }

        guard audioRestartAttempts < maxAudioRestartAttempts else {
            lifecycleLog(
                "audio restart limit reached"
            )

            emit([
                "type": "error",
                "error":
                    "Audio input repeatedly stopped after device configuration changes"
            ])

            stop(
                exitAfterStop: true,
                cancelRecognition: true
            )

            return
        }

        audioRestartScheduled = true
        audioRestartAttempts += 1

        lifecycleLog(
            "scheduling audio restart after configuration change: " +
            "attempt=\(audioRestartAttempts)"
        )

        /*
        Give CoreAudio time to finish changing the input device/profile.
        Bluetooth devices especially may briefly report an unstable format.
        */
        DispatchQueue.main.asyncAfter(
            deadline: .now() + 0.35
        ) { [weak self] in
            guard let self = self else { return }

            self.audioRestartScheduled = false

            let currentState = self.shutdownState()

            guard !currentState.isStopping else {
                return
            }

            self.restartAudioEngineAfterConfigurationChange()
        }
    }


    private func restartAudioEngineAfterConfigurationChange() {
        precondition(Thread.isMainThread)

        lifecycleLog(
            "rebuilding audio engine after " +
            "configuration change"
        )

        if audioEngine.isRunning {
            audioEngine.stop()
        }

        if tapInstalled {
            let node = (
                inputNode
                ?? audioEngine.inputNode
            )

            node.removeTap(
                onBus: 0
            )

            tapInstalled = false
        }

        inputNode = nil

        /*
        Reset stale graph state before reading the new route.
        */
        audioEngine.reset()

        do {
            _ = try validateDefaultInputHardware()

            /*
            installInputTap() obtains the input node and formats again.
            It does not reuse the old AVAudioFormat.
            */
            try installInputTap()

            audioEngine.prepare()

            try audioEngine.start()

            lifecycleLog(
                "audio engine restart returned: " +
                "running=\(audioEngine.isRunning)"
            )

            if !audioEngine.isRunning {
                scheduleAudioRestartAfterConfigurationChange()
            } else {
                /*
                A successful restart resets the retry counter.
                Otherwise three unrelated configuration changes over
                the lifetime of the helper could exhaust the limit.
                */
                audioRestartAttempts = 0
            }

        } catch {
            lifecycleLog(
                "audio engine restart failed: " +
                error.localizedDescription
            )

            if tapInstalled {
                let node = (
                    inputNode
                    ?? audioEngine.inputNode
                )

                node.removeTap(
                    onBus: 0
                )

                tapInstalled = false
            }

            inputNode = nil
            audioEngine.reset()

            scheduleAudioRestartAfterConfigurationChange()
        }
    }


    func start() {
        precondition(Thread.isMainThread)

        let initialState = shutdownState()

        guard !initialState.isStopping else {
            emit([
                "type": "error",
                "error": "Speech helper is still stopping"
            ])
            return
        }

        guard let recognizer = speechRecognizer else {
            failAndTerminate(
                message: "Failed to create SFSpeechRecognizer",
                code: 6
            )
            return
        }

        guard recognizer.isAvailable else {
            failAndTerminate(
                message: "Speech recognizer is not available",
                code: 7
            )
            return
        }

        /*
        Check the actual CoreAudio input route before creating
        the speech task.
        */
        do {
            _ = try validateDefaultInputHardware()

        } catch {
            failAndTerminate(
                message:
                    "Microphone input is unavailable: " +
                    error.localizedDescription,
                code: 10
            )

            return
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.taskHint = .dictation

        if #available(macOS 10.15, *) {
            let useOnDevice =
                ProcessInfo.processInfo.environment[
                    "SPEECH_HELPER_ON_DEVICE"
                ] == "1"

            request.requiresOnDeviceRecognition =
                useOnDevice && recognizer.supportsOnDeviceRecognition

            lifecycleLog(
                "on-device recognition: " +
                "\(request.requiresOnDeviceRecognition)"
            )
        }

        stateLock.lock()
        recognitionRequest = request
        isStopping = false
        shouldExitAfterStop = false
        stateLock.unlock()

        // 1. Create recognition task before starting AVAudioEngine.
        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self = self else { return }

            /*
            Speech callbacks are not guaranteed to run on the main thread.

            During normal recognition:
            - emit partial/final results

            During shutdown:
            - do not emit stale partial text
            - treat the cancellation/error callback as confirmation that
            SFSpeechRecognitionTask has finished unwinding
            */

            if let result = result {
                let text = result.bestTranscription.formattedString

                self.debugLog(
                    "recognition result: " +
                    "final=\(result.isFinal), " +
                    "chars=\(text.count)"
                )

                let state = self.shutdownState()

                if !state.isStopping {
                    self.emit([
                        "type": result.isFinal ? "final" : "partial",
                        "text": text
                    ])
                }

                if result.isFinal {
                    DispatchQueue.main.async { [weak self] in
                        guard let self = self else { return }

                        // Read the state again on the main queue because it may have
                        // changed since the Speech callback began.
                        let currentState = self.shutdownState()

                        if currentState.isStopping {
                            if currentState.shouldExitAfterStop {
                                self.scheduleDelayedCleanupAndExit(
                                    reason: "final result during shutdown"
                                )
                            } else {
                                self.completeCleanupWithoutExit(
                                    reason: "final result during shutdown"
                                )
                            }
                        } else {
                            // Recognition completed naturally.
                            self.stop(
                                exitAfterStop: true,
                                cancelRecognition: false
                            )
                        }
                    }

                    return
                }
            }

            if let error = error {
                let nsError = error as NSError

                self.lifecycleLog(
                    "recognition error: " +
                    "domain=\(nsError.domain), " +
                    "code=\(nsError.code), " +
                    "message=\(error.localizedDescription)"
                )

                DispatchQueue.main.async { [weak self] in
                    guard let self = self else { return }

                    // let nsError = error as NSError
                    let state = self.shutdownState()

                    if state.isStopping {
                        self.debugLog(
                            "Recognition task ended during shutdown: " +
                            "\(error.localizedDescription), code=\(nsError.code)"
                        )

                        if state.shouldExitAfterStop {
                            self.scheduleDelayedCleanupAndExit(
                                reason: "recognition cancellation callback"
                            )
                        } else {
                            self.completeCleanupWithoutExit(
                                reason: "recognition cancellation callback"
                            )
                        }

                        return
                    }

                    // This is an unexpected recognition error during an active session.
                    self.debugLog(
                        "Recognition error: \(error.localizedDescription), " +
                        "code=\(nsError.code)"
                    )

                    self.emit([
                        "type": "error",
                        "error": error.localizedDescription
                    ])

                    self.stop(
                        exitAfterStop: true,
                        cancelRecognition: true
                    )
                }
            }
        }

        // 2. Install input tap.
        do {
            try installInputTap()
        } catch {
            failAndTerminate(
                message: error.localizedDescription,
                code: 10
            )
            return
        }

        startAudioConfigurationObserver()

        // 3. Start audio engine last.
        audioEngine.prepare()

        do {
            try audioEngine.start()
        } catch {
            if tapInstalled, let node = inputNode {
                node.removeTap(onBus: 0)
                tapInstalled = false
            }

            stateLock.lock()
            let failedRequest = recognitionRequest
            recognitionRequest = nil
            isStopping = true
            shouldExitAfterStop = true
            stateLock.unlock()

            failedRequest?.endAudio()

            inputNode = nil
            audioEngine.reset()

            emit([
                "type": "error",
                "error": "Audio engine failed to start: \(error.localizedDescription)",
                "code": 9
            ])

            lifecycleLog(
                "audio engine failed to start: \(error.localizedDescription)"
            )

            scheduleCleanupFallback()

            lifecycleLog("about to cancel recognition task after start failure")

            // Dispatch cancel to background to avoid blocking main thread
            let failedTask = recognitionTask
            DispatchQueue.global(qos: .userInitiated).async {
                failedTask?.cancel()
            }

            lifecycleLog("recognition task cancel dispatched after start failure")

            return
        }

        let currentFormat = inputNode?.outputFormat(forBus: 0)

        lifecycleLog(
            "audio engine started: " +
            "running=\(audioEngine.isRunning), " +
            "sampleRate=\(currentFormat?.sampleRate ?? 0), " +
            "channels=\(currentFormat?.channelCount ?? 0)"
        )

        // startAudioDiagnostics()

        emit([
            "type": "started",
            "locale": localeIdentifier
        ])
    }


    private func scheduleCleanupFallback() {
        precondition(Thread.isMainThread)

        guard !cleanupFallbackScheduled else { return }
        cleanupFallbackScheduled = true

        lifecycleLog("cleanup fallback scheduled for 2.5 seconds")

        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            guard let self = self else { return }

            if self.finalExitStarted || self.delayedExitScheduled {
                return
            }

            self.lifecycleLog(
                "cleanup callback timeout; executing fallback"
            )

            let state = self.shutdownState()

            if state.shouldExitAfterStop {
                self.completeCleanupAndExit(
                    reason: "cleanup callback timeout"
                )
            } else {
                self.completeCleanupWithoutExit(
                    reason: "cleanup callback timeout"
                )
            }
        }
    }


    private func completeCleanupAndExit(reason: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.completeCleanupAndExit(reason: reason)
            }
            return
        }

        guard !finalExitStarted else { return }
        finalExitStarted = true

        lifecycleLog("Completing cleanup and exiting: \(reason)")

        /*
        Important:
        Set the termination flag BEFORE releasing Speech objects.

        Releasing recognitionTask can occasionally block while Apple's Speech
        framework is unwinding. The outer RunLoop must already know that shutdown
        was requested.
        */

        /*
        Wake the current RunLoop iteration. The top-level loop checks
        terminationRequested and then exits naturally.
        */
        requestTermination()

        /*
        Do not force all framework objects to deinitialize synchronously here.

        Audio input was already stopped, the tap was removed, and the engine was
        reset in stop(). Allow normal process teardown to release these objects.
        */
        // stateLock.lock()
        // recognitionRequest = nil
        // stateLock.unlock()
        // inputNode = nil

        /*
        Keep recognitionTask referenced until top-level teardown. Assigning nil
        here can synchronously enter Speech framework cleanup and block the main
        thread on some macOS states.
        */
    }


    private func completeCleanupWithoutExit(reason: String) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.completeCleanupWithoutExit(reason: reason)
            }
            return
        }

        self.debugLog("Completing cleanup without exit: \(reason)")

        stateLock.lock()
        recognitionRequest = nil
        stateLock.unlock()
        recognitionTask = nil
        inputNode = nil

        cleanupFallbackScheduled = false

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            self.stateLock.lock()
            self.shouldExitAfterStop = false
            self.isStopping = false
            self.stateLock.unlock()
        }
    }


    func stop(
        exitAfterStop: Bool,
        cancelRecognition: Bool = true
    ) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.stop(
                    exitAfterStop: exitAfterStop,
                    cancelRecognition: cancelRecognition
                )
            }
            return
        }

        stateLock.lock()

        if isStopping {
            if exitAfterStop {
                shouldExitAfterStop = true
            }

            stateLock.unlock()

            if exitAfterStop {
                scheduleCleanupFallback()
            }

            return
        }

        isStopping = true
        shouldExitAfterStop = exitAfterStop

        let request = recognitionRequest
        recognitionRequest = nil

        stateLock.unlock()

        // audioDiagnosticTimer?.cancel()
        // audioDiagnosticTimer = nil

        lifecycleLog(
            "stop started, exitAfterStop=\(exitAfterStop), " +
            "cancelRecognition=\(cancelRecognition)"
        )

        audioRestartScheduled = false

        if let observer = audioConfigurationObserver {
            NotificationCenter.default.removeObserver(observer)
            audioConfigurationObserver = nil
        }

        /*
        1. Stop audio input.
        */
        if audioEngine.isRunning {
            audioEngine.stop()
        }

        /*
        2. Remove tap exactly once.
        */
        if tapInstalled, let node = inputNode {
            node.removeTap(onBus: 0)
            tapInstalled = false
        }

        /*
        3. Reset AVAudioEngine.
        */
        audioEngine.reset()

        lifecycleLog(
            "audio engine stopped, tap removed, engine reset"
        )

        /*
        4. Tell Speech that audio has ended.
        */
        request?.endAudio()

        /*
        Schedule fallback BEFORE calling cancel().

        If cancel() or framework teardown unexpectedly blocks, the scheduling
        intent is already recorded. Note that a completely blocked main thread
        still cannot execute the timer, which is why lifecycle logs matter.
        */
        scheduleCleanupFallback()

        // Tell Python that the audio side has stopped.
        emit(["type": "stopped"])
        lifecycleLog("stopped event emitted")

        /*
        5. Request recognition cancellation.
        Dispatch to a background queue because recognitionTask.cancel()
        can block the main thread when the Speech framework is unwinding,
        preventing the RunLoop from processing the exit.
        */
        if cancelRecognition {
            lifecycleLog("about to cancel recognition task (async)")

            let task = recognitionTask
            DispatchQueue.global(qos: .userInitiated).async {
                self.lifecycleLog("cancelling recognition task on background thread")
                task?.cancel()
                self.lifecycleLog("recognition task cancel returned (background)")
            }

        } else {
            /*
            Graceful stop:
            request.endAudio() was already called above.

            Do not cancel the recognition task and do not exit yet.
            Wait for result.isFinal or an error callback.

            scheduleCleanupFallback() is already active, so the helper will still
            exit if Speech never returns a final callback.
            */
            lifecycleLog(
                "graceful recognition finish requested; waiting for final result"
            )
        }
    }
}

var globalHelper: SpeechHelper?
var signalSources: [DispatchSourceSignal] = []

func installSignalHandlers() {
    for signalNumber in [SIGINT, SIGTERM, SIGUSR1] {
        signal(signalNumber, SIG_IGN)

        let source = DispatchSource.makeSignalSource(
            signal: signalNumber,
            queue: .main
        )

        source.setEventHandler {
            globalHelper?.handleTerminationSignal(signalNumber)
        }

        source.resume()
        signalSources.append(source)
    }
}

let args = CommandLine.arguments
let locale = args.count >= 2 ? args[1] : "auto"

let helper = SpeechHelper(localeIdentifier: locale)
globalHelper = helper
installSignalHandlers()

helper.requestPermissionsAndStart()

while !helper.terminationRequested {
    autoreleasepool {
        _ = RunLoop.main.run(
            mode: .default,
            before: Date(timeIntervalSinceNow: 0.1)
        )
    }
}

fputs(
    "[SpeechHelper lifecycle] main RunLoop exited; top-level returning\n",
    stderr
)
fflush(stderr)

// Do not explicitly nil globalHelper.
// The audio engine and tap have already been stopped.
// Let process termination reclaim the remaining Speech objects.
