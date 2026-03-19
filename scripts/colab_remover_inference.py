import os
import torch
import torch.nn as nn
from torchvision import transforms
from PIL import Image
import matplotlib.pyplot as plt

# --- Same Model Architecture as training ---
class UNet(nn.Module):
    def __init__(self):
        super().__init__()
        def conv_block(in_c, out_c):
            return nn.Sequential(
                nn.Conv2d(in_c, out_c, kernel_size=3, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(out_c, out_c, kernel_size=3, padding=1),
                nn.ReLU(inplace=True)
            )
        self.enc1 = conv_block(3, 64)
        self.enc2 = conv_block(64, 128)
        self.enc3 = conv_block(128, 256)
        self.pool = nn.MaxPool2d(2)
        self.up2 = nn.ConvTranspose2d(256, 128, kernel_size=2, stride=2)
        self.dec2 = conv_block(256, 128)
        self.up1 = nn.ConvTranspose2d(128, 64, kernel_size=2, stride=2)
        self.dec1 = conv_block(128, 64)
        self.final = nn.Conv2d(64, 3, kernel_size=1)
    def forward(self, x):
        e1 = self.enc1(x)
        e2 = self.enc2(self.pool(e1))
        e3 = self.enc3(self.pool(e2))
        d2 = self.up2(e3); d2 = torch.cat([d2, e2], dim=1); d2 = self.dec2(d2)
        d1 = self.up1(d2); d1 = torch.cat([d1, e1], dim=1); d1 = self.dec1(d1)
        return torch.sigmoid(self.final(d1))

# --- Inference Logic ---
def restore_image(model_path, image_path, output_path=None):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = UNet().to(device)
    model.load_state_dict(torch.load(model_path, map_location=device))
    model.eval()

    orig_img = Image.open(image_path).convert("RGB")
    w, h = orig_img.size
    
    transform = transforms.Compose([
        transforms.Resize((512, 512)),
        transforms.ToTensor(),
    ])
    
    input_tensor = transform(orig_img).unsqueeze(0).to(device)
    
    with torch.no_grad():
        output = model(input_tensor)
    
    output_img = transforms.ToPILImage()(output.squeeze().cpu())
    output_img = output_img.resize((w, h), Image.LANCZOS)
    
    if output_path:
        output_img.save(output_path)
        print(f"[SUCCESS] Saved restored image to {output_path}")
    
    # Show comparison
    plt.figure(figsize=(12, 6))
    plt.subplot(1, 2, 1)
    plt.title("Watermarked")
    plt.imshow(orig_img)
    plt.axis("off")
    
    plt.subplot(1, 2, 2)
    plt.title("Restored")
    plt.imshow(output_img)
    plt.axis("off")
    plt.show()

# --- Executable Block for Colab ---
if __name__ == "__main__":
    try:
        from google.colab import drive
        drive.mount('/content/drive', force_remount=True)
    except:
        pass

    # 1. Update these paths
    CHECKPOINT = "/content/drive/My Drive/Newtoki/newtoki_remover.pth"
    
    # 2. Put an image file path here from your Drive
    # Example: "/content/drive/My Drive/Newtoki/Newtoki Watermark/1/Images/001.jpg"
    INPUT_IMAGE = "/content/drive/My Drive/Newtoki/Newtoki Watermark/1/Images/your_test_file.jpg"
    
    OUTPUT_IMAGE = "restored_manga.jpg"

    if os.path.exists(INPUT_IMAGE):
        restore_image(CHECKPOINT, INPUT_IMAGE, OUTPUT_IMAGE)
    else:
        print(f"[ERROR] Could not find the input image: {INPUT_IMAGE}")
        print("[TIP] Please update the 'INPUT_IMAGE' variable with a real path from your Drive.")
