// Robo's macOS camera bridge. Only video is requested; no microphone input.
import AVFoundation
import CoreImage
import Foundation

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}

func fail(_ code: String, _ message: String) -> Never {
    emit(["error": code, "message": message])
    exit(1)
}

func describe(_ device: AVCaptureDevice) -> [String: Any] {
    return ["uid": device.uniqueID, "name": device.localizedName,
            "model_id": device.modelID, "device_type": device.deviceType.rawValue,
            "built_in": device.deviceType == .builtInWideAngleCamera]
}

var deviceTypes: [AVCaptureDevice.DeviceType] = [.builtInWideAngleCamera]
if #available(macOS 14.0, *) { deviceTypes.append(.external) }
else { deviceTypes.append(.externalUnknown) }
let devices = AVCaptureDevice.DiscoverySession(
    deviceTypes: deviceTypes,
    mediaType: .video, position: .unspecified).devices
let args = CommandLine.arguments
if args.count == 2 && args[1] == "list" {
    emit(["devices": devices.map(describe),
          "authorization": AVCaptureDevice.authorizationStatus(for: .video).rawValue])
    exit(0)
}
if args.count != 3 || args[1] != "stream" {
    fail("camera_arguments", "Expected list or stream DEVICE_UID")
}
guard let device = devices.first(where: { $0.uniqueID == args[2] }) else {
    fail("camera_missing", "Selected camera is not available; run robo cameras again")
}
let authorization = AVCaptureDevice.authorizationStatus(for: .video)
if authorization == .notDetermined {
    let done = DispatchSemaphore(value: 0)
    AVCaptureDevice.requestAccess(for: .video) { _ in done.signal() }
    if done.wait(timeout: .now() + 30) == .timedOut {
        fail("camera_permission_pending", "Respond to the macOS camera permission prompt, then restart the host")
    }
}
guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
    fail("camera_permission_denied", "Enable camera access for the launching app in macOS Privacy & Security > Camera")
}

final class Frames: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let context = CIContext(options: [.useSoftwareRenderer: true])
    let color = CGColorSpaceCreateDeviceRGB()
    var last = -Double.infinity
    var sequence = 0

    func captureOutput(_ output: AVCaptureOutput, didOutput buffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let imageBuffer = CMSampleBufferGetImageBuffer(buffer) else { return }
        let sourceTime = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(buffer))
        if !sourceTime.isFinite || sourceTime - last < 0.18 { return }
        last = sourceTime
        let received = Date().timeIntervalSince1970
        let hostTime = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
        let source = CIImage(cvPixelBuffer: imageBuffer)
        let width = 320
        let height = max(2, Int((Double(width) * source.extent.height / source.extent.width).rounded()) / 2 * 2)
        let scaled = source.transformed(by: CGAffineTransform(
            scaleX: CGFloat(width) / source.extent.width, y: CGFloat(height) / source.extent.height))
        var rgba = [UInt8](repeating: 0, count: width * height * 4)
        rgba.withUnsafeMutableBytes { bytes in
            context.render(scaled, toBitmap: bytes.baseAddress!, rowBytes: width * 4,
                           bounds: CGRect(x: 0, y: 0, width: width, height: height),
                           format: .RGBA8, colorSpace: color)
        }
        var rgb = [UInt8]()
        rgb.reserveCapacity(width * height * 3)
        // Bitmap rows from this CVPixelBuffer render already use top-left order.
        for y in 0..<height {
            for x in 0..<width {
                let i = (y * width + x) * 4
                rgb.append(contentsOf: rgba[i..<(i + 3)])
            }
        }
        sequence += 1
        let age = hostTime - sourceTime
        // Keep old timestamps old; receive time cannot prove capture freshness.
        let mapped = age.isFinite && age >= 0
        emit(["type": "frame", "seq": sequence, "width": width, "height": height,
              "source_width": CVPixelBufferGetWidth(imageBuffer),
              "source_height": CVPixelBufferGetHeight(imageBuffer),
              "source_pts_s": sourceTime, "driver_received_at": received,
              "sampled_at": mapped ? received - age : received,
              "timestamp_kind": mapped ? "avfoundation-host-clock-mapped" : "driver-receive-estimate",
              "clock_mapping_valid": mapped, "rgb": Data(rgb).base64EncodedString()])
    }
}

let session = AVCaptureSession()
session.beginConfiguration()
if session.canSetSessionPreset(.hd1280x720) { session.sessionPreset = .hd1280x720 }
do {
    let input = try AVCaptureDeviceInput(device: device)
    guard session.canAddInput(input) else { fail("camera_input", "Cannot attach selected camera") }
    session.addInput(input)
} catch {
    fail("camera_input", error.localizedDescription)
}
let output = AVCaptureVideoDataOutput()
output.alwaysDiscardsLateVideoFrames = true
output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
guard session.canAddOutput(output) else { fail("camera_output", "Camera has no usable video output") }
session.addOutput(output)
if let connection = output.connection(with: .video), connection.isVideoMirroringSupported {
    connection.automaticallyAdjustsVideoMirroring = false
    connection.isVideoMirrored = false
}
let frames = Frames()
output.setSampleBufferDelegate(frames, queue: DispatchQueue(label: "robo.camera.frames"))
session.commitConfiguration()

// Close the camera if the execution host dies, even without a graceful stop.
DispatchQueue.global().async {
    _ = FileHandle.standardInput.readData(ofLength: 1)
    exit(0)
}
session.startRunning()
RunLoop.main.run()
