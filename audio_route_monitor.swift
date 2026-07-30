import Foundation
import CoreAudio
import Darwin

final class AudioRouteMonitor {
    private let systemObjectID =
        AudioObjectID(kAudioObjectSystemObject)

    private let listenerQueue = DispatchQueue(
        label: "local.voice.audio-route-monitor"
    )

    private var defaultOutputAddress =
        AudioObjectPropertyAddress(
            mSelector:
                kAudioHardwarePropertyDefaultOutputDevice,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

    private var defaultInputAddress =
        AudioObjectPropertyAddress(
            mSelector:
                kAudioHardwarePropertyDefaultInputDevice,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

    private var devicesAddress =
        AudioObjectPropertyAddress(
            mSelector:
                kAudioHardwarePropertyDevices,
            mScope:
                kAudioObjectPropertyScopeGlobal,
            mElement:
                kAudioObjectPropertyElementMain
        )

    private var outputListener:
        AudioObjectPropertyListenerBlock?

    private var inputListener:
        AudioObjectPropertyListenerBlock?

    private var devicesListener:
        AudioObjectPropertyListenerBlock?

    private var outputRegistered = false
    private var inputRegistered = false
    private var devicesRegistered = false

    private let outputLock = NSLock()

    func start() throws {
        outputListener = makeListener(
            fallbackEvent: "default_output_changed"
        )

        inputListener = makeListener(
            fallbackEvent: "default_input_changed"
        )

        devicesListener = makeListener(
            fallbackEvent: "devices_changed"
        )

        guard let outputListener else {
            throw MonitorError.listenerCreationFailed(
                "default output"
            )
        }

        guard let inputListener else {
            throw MonitorError.listenerCreationFailed(
                "default input"
            )
        }

        guard let devicesListener else {
            throw MonitorError.listenerCreationFailed(
                "devices"
            )
        }

        var status = AudioObjectAddPropertyListenerBlock(
            systemObjectID,
            &defaultOutputAddress,
            listenerQueue,
            outputListener
        )

        guard status == noErr else {
            throw MonitorError.registrationFailed(
                property: "default output",
                status: status
            )
        }

        outputRegistered = true

        status = AudioObjectAddPropertyListenerBlock(
            systemObjectID,
            &defaultInputAddress,
            listenerQueue,
            inputListener
        )

        guard status == noErr else {
            throw MonitorError.registrationFailed(
                property: "default input",
                status: status
            )
        }

        inputRegistered = true

        status = AudioObjectAddPropertyListenerBlock(
            systemObjectID,
            &devicesAddress,
            listenerQueue,
            devicesListener
        )

        guard status == noErr else {
            throw MonitorError.registrationFailed(
                property: "devices",
                status: status
            )
        }

        devicesRegistered = true

        emitEvent(
            type: "started",
            selector: "monitor",
            addressCount: 0
        )
    }

    func stop() {
        if outputRegistered,
           let outputListener {
            let status =
                AudioObjectRemovePropertyListenerBlock(
                    systemObjectID,
                    &defaultOutputAddress,
                    listenerQueue,
                    outputListener
                )

            if status != noErr {
                emitError(
                    message:
                        "Failed removing output listener",
                    status: status
                )
            }
        }

        if inputRegistered,
           let inputListener {
            let status =
                AudioObjectRemovePropertyListenerBlock(
                    systemObjectID,
                    &defaultInputAddress,
                    listenerQueue,
                    inputListener
                )

            if status != noErr {
                emitError(
                    message:
                        "Failed removing input listener",
                    status: status
                )
            }
        }

        if devicesRegistered,
           let devicesListener {
            let status =
                AudioObjectRemovePropertyListenerBlock(
                    systemObjectID,
                    &devicesAddress,
                    listenerQueue,
                    devicesListener
                )

            if status != noErr {
                emitError(
                    message:
                        "Failed removing devices listener",
                    status: status
                )
            }
        }

        outputRegistered = false
        inputRegistered = false
        devicesRegistered = false

        emitEvent(
            type: "stopped",
            selector: "monitor",
            addressCount: 0
        )
    }

    private func makeListener(
        fallbackEvent: String
    ) -> AudioObjectPropertyListenerBlock {
        return {
            [weak self]
            numberAddresses,
            addresses
            in

            guard let self else {
                return
            }

            if numberAddresses == 0 {
                self.emitEvent(
                    type: "audio_route_changed",
                    selector: fallbackEvent,
                    addressCount: 0
                )

                return
            }

            for index in 0..<Int(numberAddresses) {
                let address = addresses[index]

                self.emitEvent(
                    type: "audio_route_changed",
                    selector:
                        self.selectorName(
                            address.mSelector,
                            fallback:
                                fallbackEvent
                        ),
                    addressCount:
                        numberAddresses
                )
            }
        }
    }

    private func selectorName(
        _ selector:
            AudioObjectPropertySelector,
        fallback: String
    ) -> String {
        switch selector {
        case
            kAudioHardwarePropertyDefaultOutputDevice:
            return "default_output"

        case
            kAudioHardwarePropertyDefaultInputDevice:
            return "default_input"

        case kAudioHardwarePropertyDevices:
            return "devices"

        default:
            return fallback
        }
    }

    private func currentDefaultDeviceID(
        selector:
            AudioObjectPropertySelector
    ) -> AudioDeviceID? {
        var address =
            AudioObjectPropertyAddress(
                mSelector: selector,
                mScope:
                    kAudioObjectPropertyScopeGlobal,
                mElement:
                    kAudioObjectPropertyElementMain
            )

        var deviceID =
            AudioDeviceID(kAudioObjectUnknown)

        var dataSize = UInt32(
            MemoryLayout<AudioDeviceID>.size
        )

        let status =
            AudioObjectGetPropertyData(
                systemObjectID,
                &address,
                0,
                nil,
                &dataSize,
                &deviceID
            )

        guard
            status == noErr,
            deviceID
                != AudioDeviceID(
                    kAudioObjectUnknown
                )
        else {
            return nil
        }

        return deviceID
    }

    private func deviceName(
        deviceID: AudioDeviceID
    ) -> String? {
        var address =
            AudioObjectPropertyAddress(
                mSelector:
                    kAudioObjectPropertyName,
                mScope:
                    kAudioObjectPropertyScopeGlobal,
                mElement:
                    kAudioObjectPropertyElementMain
            )

        var value:
            Unmanaged<CFString>?

        var dataSize = UInt32(
            MemoryLayout<
                Unmanaged<CFString>?
            >.size
        )

        let status =
            AudioObjectGetPropertyData(
                deviceID,
                &address,
                0,
                nil,
                &dataSize,
                &value
            )

        guard
            status == noErr,
            let value
        else {
            return nil
        }

        return value
            .takeUnretainedValue()
            as String
    }

    private func outputSnapshot()
        -> [String: Any] {
        var snapshot:
            [String: Any] = [:]

        if let outputID =
            currentDefaultDeviceID(
                selector:
                    kAudioHardwarePropertyDefaultOutputDevice
            ) {
            snapshot[
                "default_output_id"
            ] = outputID

            if let name = deviceName(
                deviceID: outputID
            ) {
                snapshot[
                    "default_output_name"
                ] = name
            }
        }

        if let inputID =
            currentDefaultDeviceID(
                selector:
                    kAudioHardwarePropertyDefaultInputDevice
            ) {
            snapshot[
                "default_input_id"
            ] = inputID

            if let name = deviceName(
                deviceID: inputID
            ) {
                snapshot[
                    "default_input_name"
                ] = name
            }
        }

        return snapshot
    }

    private func emitEvent(
        type: String,
        selector: String,
        addressCount: UInt32
    ) {
        var payload:
            [String: Any] = [
                "type": type,
                "selector": selector,
                "address_count": addressCount,
                "timestamp":
                    Date().timeIntervalSince1970,
            ]

        for (
            key,
            value
        ) in outputSnapshot() {
            payload[key] = value
        }

        emitJSON(
            payload
        )
    }

    private func emitError(
        message: String,
        status: OSStatus
    ) {
        emitJSON(
            [
                "type": "error",
                "message": message,
                "os_status": status,
                "timestamp":
                    Date().timeIntervalSince1970,
            ]
        )
    }

    private func emitJSON(
        _ payload: [String: Any]
    ) {
        outputLock.lock()

        defer {
            outputLock.unlock()
        }

        do {
            let data =
                try JSONSerialization.data(
                    withJSONObject:
                        payload,
                    options: []
                )

            if let line = String(
                data: data,
                encoding: .utf8
            ) {
                FileHandle.standardOutput.write(
                    Data(
                        (line + "\n").utf8
                    )
                )
            }

        } catch {
            let line =
                #"{"type":"error","message":"JSON serialization failed"}"#

            FileHandle.standardOutput.write(
                Data(
                    (line + "\n").utf8
                )
            )
        }
    }
}

enum MonitorError:
    LocalizedError {
    case listenerCreationFailed(
        String
    )

    case registrationFailed(
        property: String,
        status: OSStatus
    )

    var errorDescription: String? {
        switch self {
        case let .listenerCreationFailed(
            property
        ):
            return
                "Unable to create listener for "
                + property

        case let .registrationFailed(
            property,
            status
        ):
            return
                "Unable to register listener for "
                + property
                + "; OSStatus="
                + String(status)
        }
    }
}

let monitor = AudioRouteMonitor()

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)

let signalQueue = DispatchQueue(
    label: "local.voice.audio-route-signals"
)

let terminationSource =
    DispatchSource.makeSignalSource(
        signal: SIGTERM,
        queue: signalQueue
    )

let interruptSource =
    DispatchSource.makeSignalSource(
        signal: SIGINT,
        queue: signalQueue
    )

func stopAndExit() {
    monitor.stop()
    exit(0)
}

terminationSource.setEventHandler {
    stopAndExit()
}

interruptSource.setEventHandler {
    stopAndExit()
}

terminationSource.resume()
interruptSource.resume()

do {
    try monitor.start()

    dispatchMain()

} catch {
    let message =
        error.localizedDescription
        .replacingOccurrences(
            of: "\\",
            with: "\\\\"
        )
        .replacingOccurrences(
            of: "\"",
            with: "\\\""
        )

    let line =
        "{\"type\":\"fatal_error\","
        + "\"message\":\""
        + message
        + "\"}\n"

    FileHandle.standardOutput.write(
        Data(line.utf8)
    )

    exit(1)
}
