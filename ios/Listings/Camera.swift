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

    private func configure() {
        session.beginConfiguration()
        session.sessionPreset = .photo
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input), session.canAddOutput(output) else {
            session.commitConfiguration()
            DispatchQueue.main.async { self.available = false }  // the simulator has no camera
            return
        }
        session.addInput(input)
        session.addOutput(output)
        session.commitConfiguration()
        configured = true
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
    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.preview.session = session
        v.preview.videoGravity = .resizeAspectFill
        return v
    }
    func updateUIView(_ uiView: PreviewView, context: Context) {}
}
