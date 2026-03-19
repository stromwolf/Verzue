import os
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, models
from PIL import Image
import torch.nn.functional as F
from tqdm import tqdm

# --- Configuration ---
# Update these if your Drive paths are different
WATERMARK_DIR = "/content/drive/My Drive/Newtoki/Train_Input_Watermarked"
ORIGINAL_DIR = "/content/drive/My Drive/Newtoki/Train_Output_Clean"
CHECKPOINT_PATH = "/content/drive/My Drive/Newtoki/newtoki_remover.pth"

BATCH_SIZE = 8
LR = 1e-4
EPOCHS = 50
IMG_SIZE = (512, 512)

# --- SSIM Loss ---
def ssim(img1, img2, window_size=11, size_average=True):
    # Basic SSIM implementation for PyTorch
    C1 = (0.01 * 255)**2
    C2 = (0.03 * 255)**2
    
    mu1 = F.avg_pool2d(img1, window_size, stride=1, padding=window_size//2)
    mu2 = F.avg_pool2d(img2, window_size, stride=1, padding=window_size//2)
    
    mu1_sq = mu1.pow(2)
    mu2_sq = mu2.pow(2)
    mu1_mu2 = mu1 * mu2
    
    sigma1_sq = F.avg_pool2d(img1 * img1, window_size, stride=1, padding=window_size//2) - mu1_sq
    sigma2_sq = F.avg_pool2d(img2 * img2, window_size, stride=1, padding=window_size//2) - mu2_sq
    sigma12 = F.avg_pool2d(img1 * img2, window_size, stride=1, padding=window_size//2) - mu1_mu2
    
    ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))
    
    if size_average:
        return ssim_map.mean()
    else:
        return ssim_map.mean(1).mean(1).mean(1)

class WatermarkLoss(nn.Module):
    def __init__(self):
        super().__init__()
        self.l1 = nn.L1Loss()
        
    def forward(self, pred, target):
        l1_loss = self.l1(pred, target)
        ssim_loss = 1 - ssim(pred, target)
        return 0.8 * l1_loss + 0.2 * ssim_loss

# --- Dataset ---
class MangaDataset(Dataset):
    def __init__(self, watermark_dir, original_dir, transform=None):
        self.watermark_dir = watermark_dir
        self.original_dir = original_dir
        self.transform = transform
        
        if not os.path.exists(watermark_dir) or not os.path.exists(original_dir):
            print(f"[ERROR] Training directories not found!")
            print(f"Expected: {watermark_dir}")
            print(f"Please run align_manga_data.py first.")
            self.filenames = []
        else:
            # Recursively find all image files and store their relative paths
            self.filenames = []
            for root, dirs, files in os.walk(watermark_dir):
                for f in files:
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                        rel_path = os.path.relpath(os.path.join(root, f), watermark_dir)
                        if os.path.exists(os.path.join(original_dir, rel_path)):
                            self.filenames.append(rel_path)
            
        print(f"[INFO] Found {len(self.filenames)} paired images.")

    def __len__(self):
        return len(self.filenames)

    def __getitem__(self, idx):
        name = self.filenames[idx]
        wm_path = os.path.join(self.watermark_dir, name)
        orig_path = os.path.join(self.original_dir, name)
        
        wm_img = Image.open(wm_path).convert("RGB")
        orig_img = Image.open(orig_path).convert("RGB")
        
        if self.transform:
            wm_img = self.transform(wm_img)
            orig_img = self.transform(orig_img)
            
        return wm_img, orig_img

# --- Simple U-Net ---
class UNet(nn.Module):
    def __init__(self):
        super().__init__()
        # Using a simplified encoder-decoder structure
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
        
        d2 = self.up2(e3)
        d2 = torch.cat([d2, e2], dim=1)
        d2 = self.dec2(d2)
        
        d1 = self.up1(d2)
        d1 = torch.cat([d1, e1], dim=1)
        d1 = self.dec1(d1)
        
        return torch.sigmoid(self.final(d1))

# --- Main Logic ---
def train():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[INFO] Using device: {device}")

    transform = transforms.Compose([
        transforms.Resize(IMG_SIZE),
        transforms.ToTensor(),
    ])

    dataset = MangaDataset(WATERMARK_DIR, ORIGINAL_DIR, transform=transform)
    dataloader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True)

    model = UNet().to(device)
    optimizer = optim.Adam(model.parameters(), lr=LR)
    criterion = WatermarkLoss()

    if os.path.exists(CHECKPOINT_PATH):
        print("[INFO] Resuming from checkpoint...")
        model.load_state_dict(torch.load(CHECKPOINT_PATH, map_location=device))

    for epoch in range(EPOCHS):
        model.train()
        running_loss = 0.0
        pbar = tqdm(dataloader, desc=f"Epoch {epoch+1}/{EPOCHS}")
        
        for wm, orig in pbar:
            wm, orig = wm.to(device), orig.to(device)
            
            optimizer.zero_grad()
            outputs = model(wm)
            loss = criterion(outputs, orig)
            loss.backward()
            optimizer.step()
            
            running_loss += loss.item()
            pbar.set_postfix({"loss": f"{loss.item():.4f}"})
            
        avg_loss = running_loss / len(dataloader)
        print(f"Epoch {epoch+1} Complete. Avg Loss: {avg_loss:.4f}")
        
        # Save checkpoint to Drive
        torch.save(model.state_dict(), CHECKPOINT_PATH)
        print(f"[INFO] Saved checkpoint to {CHECKPOINT_PATH}")

if __name__ == "__main__":
    from google.colab import drive
    drive.mount('/content/drive')
    train()
