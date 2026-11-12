import SwiftUI

/// A photo from the server (they need the sign-in token, so AsyncImage can't load them).
struct AuthImage: View {
    let path: String
    @State private var image: UIImage?
    private static var cache = NSCache<NSString, UIImage>()

    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().scaledToFill() } else { Color.gray.opacity(0.2) }
        }
        .task(id: path) {
            if let hit = Self.cache.object(forKey: path as NSString) { image = hit; return }
            if let data = try? await API.shared.request("GET", path), let img = UIImage(data: data) {
                Self.cache.setObject(img, forKey: path as NSString)
                image = img
            }
        }
    }
}
