import AVFoundation
import SwiftUI
import UIKit

/// The back camera, kept running while shooting a tote so each photo is one tap.
final class Camera: NSObject, ObservableObject, AVCapturePhotoCaptureDelegate {
    let session = AVCaptureSession()
    private let output = AVCapturePhotoOutput()
    private var configured = false
    private var waiting: ((Data?) -> Void)?
    @Published var available = true
    @Published var denied = false

    func start() {
        AVCaptureDevice.requestAccess(for: .video) { ok in
            DispatchQueue.main.async { self.denied = !ok }
            guard ok else { return }
            DispatchQueue.global(qos: .userInitiated).async {
                if !self.configured { self.configure() }
                if !self.session.isRunning { self.session.startRunning() }
            }
        }
    }

    func stop() { DispatchQueue.global(qos: .userInitiated).async { self.session.stopRunning() } }

    private var device: AVCaptureDevice?

    /// The multi-lens camera where there is one. On recent iPhones the main lens can't focus closer than
    /// about 20 cm, so a small item shot up close stays blurry; the multi-lens camera switches to the
    /// ultra-wide lens's macro mode by itself, like Apple's Camera app. Apps that open only the main lens
    /// can't focus up close either.
    private static func bestBackCamera() -> AVCaptureDevice? {
        for type in [AVCaptureDevice.DeviceType.builtInTripleCamera, .builtInDualWideCamera, .builtInDualCamera, .builtInWideAngleCamera] {
            if let d = AVCaptureDevice.default(type, for: .video, position: .back) { return d }
        }
        return nil
    }

    private func configure() {
        session.beginConfiguration()
        session.sessionPreset = .photo
        guard let device = Self.bestBackCamera(),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input), session.canAddOutput(output) else {
            session.commitConfiguration()
            DispatchQueue.main.async { self.available = false }  // the simulator has no camera
            return
        }
        session.addInput(input)
        session.addOutput(output)
        output.maxPhotoQualityPrioritization = .balanced
        session.commitConfiguration()
        self.device = device
        if (try? device.lockForConfiguration()) != nil {
            if device.isVirtualDevice {
                // Start at the main lens's view (1x), as the Camera app does; zoom factor 1 is the ultra-wide.
                if let wide = device.virtualDeviceSwitchOverVideoZoomFactors.first {
                    device.videoZoomFactor = CGFloat(truncating: wide)
                }
                device.setPrimaryConstituentDeviceSwitchingBehavior(.auto, restrictedSwitchingBehaviorConditions: [])
            }
            if device.isAutoFocusRangeRestrictionSupported { device.autoFocusRangeRestriction = .near }  // items are close
            continuous(device)
            device.isSubjectAreaChangeMonitoringEnabled = true
            device.unlockForConfiguration()
        }
        NotificationCenter.default.addObserver(forName: AVCaptureDevice.subjectAreaDidChangeNotification, object: device,
                                               queue: nil) { [weak self] _ in self?.refocus() }
        configured = true
    }

    private func continuous(_ device: AVCaptureDevice) {
        if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
        if device.isExposureModeSupported(.continuousAutoExposure) { device.exposureMode = .continuousAutoExposure }
    }

    /// Tap to focus: focus and expose on that spot (0-1 device coordinates) until the scene changes.
    func focus(at point: CGPoint) {
        guard let device else { return }
        DispatchQueue.global(qos: .userInitiated).async {
            guard (try? device.lockForConfiguration()) != nil else { return }
            if device.isFocusPointOfInterestSupported && device.isFocusModeSupported(.autoFocus) {
                device.focusPointOfInterest = point
                device.focusMode = .autoFocus
            }
            if device.isExposurePointOfInterestSupported && device.isExposureModeSupported(.autoExpose) {
                device.exposurePointOfInterest = point
                device.exposureMode = .autoExpose
            }
            device.unlockForConfiguration()
        }
    }

    /// The camera moved or the item changed after a tap: back to focusing on the middle by itself.
    private func refocus() {
        guard let device, (try? device.lockForConfiguration()) != nil else { return }
        if device.isFocusPointOfInterestSupported { device.focusPointOfInterest = CGPoint(x: 0.5, y: 0.5) }
        if device.isExposurePointOfInterestSupported { device.exposurePointOfInterest = CGPoint(x: 0.5, y: 0.5) }
        continuous(device)
        device.unlockForConfiguration()
    }

    /// Take a photo; the reply is a JPEG sized for listings, or nil if it failed.
    func shoot(_ done: @escaping (Data?) -> Void) {
        guard configured else { done(nil); return }
        waiting = done
        output.capturePhoto(with: AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg]), delegate: self)
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let data = photo.fileDataRepresentation().flatMap(listingJPEG)
        DispatchQueue.main.async { self.waiting?(data); self.waiting = nil }
    }
}

/// Any image (camera, library, HEIC) as a JPEG of at most 2400 px on the long edge: plenty for eBay,
/// quick to upload, and a format the drafter can read.
func listingJPEG(_ data: Data) -> Data? {
    guard let image = UIImage(data: data) else { return nil }
    let longest = max(image.size.width, image.size.height)
    let scale = min(1, 2400 / longest)
    let size = CGSize(width: (image.size.width * scale).rounded(), height: (image.size.height * scale).rounded())
    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    let resized = UIGraphicsImageRenderer(size: size, format: format).image { _ in image.draw(in: CGRect(origin: .zero, size: size)) }
    return resized.jpegData(compressionQuality: 0.85)
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    var onTap: (CGPoint) -> Void = { _ in }

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
        var onTap: (CGPoint) -> Void = { _ in }
        private let square = UIView(frame: CGRect(x: 0, y: 0, width: 80, height: 80))

        override init(frame: CGRect) {
            super.init(frame: frame)
            square.layer.borderColor = UIColor.systemYellow.cgColor
            square.layer.borderWidth = 2
            square.alpha = 0
            square.isUserInteractionEnabled = false
            addSubview(square)
            addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped)))
        }
        required init?(coder: NSCoder) { fatalError("not used") }

        @objc private func tapped(_ tap: UITapGestureRecognizer) {
            let at = tap.location(in: self)
            onTap(preview.captureDevicePointConverted(fromLayerPoint: at))
            square.center = at
            square.alpha = 1
            UIView.animate(withDuration: 0.3, delay: 0.7, options: []) { self.square.alpha = 0 }
        }
    }
    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.preview.session = session
        v.preview.videoGravity = .resizeAspectFill
        v.onTap = onTap
        return v
    }
    func updateUIView(_ uiView: PreviewView, context: Context) { uiView.onTap = onTap }
}
