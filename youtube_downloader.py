"""
YouTube Audio Downloader Module

Provides functionality to validate YouTube URLs and download audio as high-quality MP3.
"""

import re
import os
from urllib.parse import urlparse, parse_qs
from typing import Optional
import yt_dlp


def validate_youtube_url(url: str) -> Optional[str]:
    """
    Validates that a URL is a legitimate YouTube URL and extracts the video ID.
    
    Args:
        url: The URL to validate
        
    Returns:
        The video ID if valid, None otherwise
        
    Raises:
        ValueError: If the URL is not a valid YouTube URL
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    
    # Clean the URL
    url = url.strip()
    
    # List of valid YouTube domains
    valid_domains = [
        'youtube.com',
        'www.youtube.com',
        'm.youtube.com',
        'youtu.be',
        'www.youtu.be',
        'music.youtube.com',
    ]
    
    try:
        parsed = urlparse(url)
        
        # Ensure scheme is http or https (or empty for shorthand)
        if parsed.scheme and parsed.scheme not in ('http', 'https'):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed.")
        
        # Check if the domain is valid
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("Could not parse hostname from URL")
            
        hostname = hostname.lower()
        
        if hostname not in valid_domains:
            raise ValueError(f"Invalid domain: {hostname}. Not a recognized YouTube domain.")
        
        # Extract video ID based on URL format
        video_id = None
        
        # Format: youtu.be/VIDEO_ID
        if hostname in ('youtu.be', 'www.youtu.be'):
            path = parsed.path.strip('/')
            if path:
                video_id = path.split('/')[0].split('?')[0]
        
        # Format: youtube.com/watch?v=VIDEO_ID
        elif 'watch' in parsed.path:
            query_params = parse_qs(parsed.query)
            if 'v' in query_params:
                video_id = query_params['v'][0]
        
        # Format: youtube.com/embed/VIDEO_ID or youtube.com/v/VIDEO_ID
        elif '/embed/' in parsed.path or '/v/' in parsed.path:
            path_parts = parsed.path.split('/')
            for i, part in enumerate(path_parts):
                if part in ('embed', 'v') and i + 1 < len(path_parts):
                    video_id = path_parts[i + 1]
                    break
        
        # Format: youtube.com/shorts/VIDEO_ID
        elif '/shorts/' in parsed.path:
            path_parts = parsed.path.split('/')
            for i, part in enumerate(path_parts):
                if part == 'shorts' and i + 1 < len(path_parts):
                    video_id = path_parts[i + 1]
                    break
        
        if not video_id:
            raise ValueError("Could not extract video ID from URL")
        
        # Validate video ID format (YouTube IDs are 11 characters, alphanumeric with - and _)
        video_id_pattern = re.compile(r'^[a-zA-Z0-9_-]{11}$')
        if not video_id_pattern.match(video_id):
            raise ValueError(f"Invalid video ID format: {video_id}")
        
        return video_id
        
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Failed to parse URL: {str(e)}")


def get_video_info(url: str) -> dict:
    """
    Extracts metadata from a YouTube video.
    
    Args:
        url: The YouTube video URL
        
    Returns:
        Dictionary containing title, thumbnail, and duration
    """
    validate_youtube_url(url)
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "author": info.get("uploader"),
                "view_count": info.get("view_count"),
                "video_id": info.get("id")
            }
    except Exception as e:
        raise RuntimeError(f"Failed to extract video info: {str(e)}")


def download_youtube_audio(
    url: str,
    output_dir: str = ".",
    filename: Optional[str] = None,
    progress_hook: Optional[callable] = None
) -> str:
    """
    Downloads audio from a YouTube video in the highest quality and saves it as MP3.
    
    Args:
        url: The YouTube video URL
        output_dir: Directory to save the MP3 file (default: current directory)
        filename: Optional custom filename (without extension). If not provided,
                  uses the video title.
                  
    Returns:
        The path to the downloaded MP3 file
        
    Raises:
        ValueError: If the URL is invalid
        RuntimeError: If the download fails
    """
    # Validate the URL first
    video_id = validate_youtube_url(url)
    
    # Ensure output directory exists
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    # Configure yt-dlp options for highest quality audio
    ydl_opts = {
        # Extract audio only
        'format': 'bestaudio/best',
        
        # Post-processing to convert to MP3
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',  # Highest MP3 quality (320 kbps)
        }],
        
        # Output template
        'outtmpl': os.path.join(
            output_dir,
            filename if filename else '%(title)s'
        ) + '.%(ext)s',
        
        # Don't print to stdout
        'quiet': True,
        'no_warnings': True,
        
        # Embed metadata
        'addmetadata': True,
        
        # Prevent downloading playlists
        'noplaylist': True,
        
        # Progress hook
        'progress_hooks': [progress_hook] if progress_hook else [],
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extract info first to get the final filename
            info = ydl.extract_info(url, download=True)
            
            # Construct the output path
            if filename:
                output_path = os.path.join(output_dir, f"{filename}.mp3")
            else:
                # Use the sanitized title from yt-dlp
                title = ydl.prepare_filename(info)
                # Replace the original extension with .mp3
                output_path = os.path.splitext(title)[0] + '.mp3'
            
            if not os.path.exists(output_path):
                # Try to find the file (yt-dlp might have sanitized the filename)
                for f in os.listdir(output_dir):
                    if f.endswith('.mp3') and video_id in f or info.get('title', '') in f:
                        output_path = os.path.join(output_dir, f)
                        break
            
            return output_path
            
    except yt_dlp.DownloadError as e:
        raise RuntimeError(f"Failed to download audio: {str(e)}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error during download: {str(e)}")


# Example usage
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python youtube_downloader.py <youtube_url> [output_dir] [filename]")
        sys.exit(1)
    
    url = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    filename = sys.argv[3] if len(sys.argv) > 3 else None
    
    try:
        print(f"Validating URL: {url}")
        video_id = validate_youtube_url(url)
        print(f"Valid YouTube video ID: {video_id}")
        
        print(f"Downloading audio to: {output_dir}")
        output_path = download_youtube_audio(url, output_dir, filename)
        print(f"Successfully downloaded: {output_path}")
        
    except ValueError as e:
        print(f"Validation error: {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"Download error: {e}")
        sys.exit(1)
